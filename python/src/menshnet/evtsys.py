"""
Networking module for library, this leverages mqtt and requests
"""

import paho.mqtt.client as mqtt
import queue
import requests
import json
import threading
import uuid
import logging
import os
import sys

REMOTE_HOST="menshnet.online"
VALIDATE_API_URL="https://menshnet.online/landing/api/access-mqtt"



Logger = logging.getLogger("menshnet")


class MqttTransaction(object):
    """
         Sender              MQTT   Receiver
           
         create response
         topic, subscribe ---->|

         subscribed.      <----
         send message     -----+----> process message
                                      send reply to reply
                                      topic
         route response
         end transaction <-----+-----

         unsubscribe to
         response topic  ----->| 

    """
    def __init__(self, messenger, topic, data):
        assert type(data) == type({})
        self.messenger = messenger
        self.topic = topic
        self.data = data
        self.error = None
        self.complete = threading.Event()
        self.reply = None

    # 
    # user defined event handlers
    #
    def on_result(self, mqtt_msg_payload):
        self.reply = mqtt_msg_payload

    def on_success(self):
        pass
        
    def on_failure(self, error):
        Logger.error(error)
    #############################
    


    def wait_for_completion(self, timeout):
        "wait for transaction to complete or timeout"
        Logger.info("waiting for completion")
        if self.complete.wait(timeout):
            if self.error:
                self.on_failure(self.error)
            else:
                self.on_success()
        else:
            self.on_failure("timeout")          

    def on_error(self, msg):
        "route error to on_failure set complete mutex"
        Logger.error("on_error: " + msg)
        self.error = msg         
        self.complete.set()

        # remove if reply topic exists
        if self.data.get('reply_topic'): 
            self.messenger.mqttc.message_callback_remove(self.data['reply_topic'])
            self.messenger.mqttc.unsubscribe(self.data['reply_topic'])

    def reply_handler(self, client, userdata, msg):
        "route inbound message to on_result"
        Logger.debug("received reply from remote service")
        
        self.on_result(msg.payload)
          
        self.messenger.mqttc.message_callback_remove(self.data['reply_topic'])
        self.messenger.mqttc.unsubscribe(self.data['reply_topic']) 

        # unblock and synchronous caller.
        self.complete.set()



    def begin(self):
        "begin transaction"
        Logger.debug("Beginning transaction")
        
        if not self.messenger.mqttc:
            self.on_error("mqtt not initialized") 
            return

        # reply_topic -> menshnet/client/<username>/tx-reply/<uuid>
        self.data['reply_topic'] = self.messenger.topic_base+"/tx-reply/"+str(uuid.uuid4()) 

        # subscribe to topic
        self.messenger.mqttc.subscribe(self.data['reply_topic'])
        self.messenger.mqttc.message_callback_add(self.data['reply_topic'], self.reply_handler)
        # send to remote host
        
        self.messenger.send(self.topic, self.data)

        

class Messenger(object):
    def __init__(self, user):
        self.user = user
        self.topic_base = "menshnet/client/%s" % self.user
        # create a semaphore to block until mqtt connection,
        # the mqtt client will auto reconnect until success.
        self.connected = threading.Event()
        self.connected.clear()     
        self.on_connected = None
        self.mqttc = None

    def on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            # if provided call user handler when mqtt has connected.
            if self.on_connected:
                self.on_connected()
            # set connected mutex 
            self.connected.set()


    def send(self, topic, data):
        if not self.mqttc:
            raise RuntimeError("connect must be called before using this method")
        print("mqttc.publish('%s',payload='%s')" % (topic,str(data)))
        self.mqttc.publish(topic,payload=json.dumps(data))

    def on_message(self, client, userdata, msg):
        print(msg.topic + " -> " + msg.payload.decode() )
        
    def connect(self, mqtt_auth, on_connected):
        "connect to mqtt using authentication"
        Logger.debug("connecting to mqtt using credentials")
        self.on_connected = on_connected
        self.mqttc = mqtt.Client(client_id=str(uuid.uuid4()), transport="websockets")
        self.mqttc.on_connect = self.on_connect
        self.mqttc.on_message = self.on_message
        
        self.mqttc.tls_set()
        
        u = mqtt_auth['name']
        p = mqtt_auth['pwhash']

        

        self.mqttc.username_pw_set(u, p)
        self.mqttc.ws_set_options()
        self.mqttc.connect(REMOTE_HOST, 443, 60)

        # execute daemon thread for network communications to the
        # MQTT broker 
        self.mqttc.loop_start()


class EventSystem(object):
    """
    Wrapper aroung MQTT for messaging.
    """
    def __init__(self):
        self.messenger = None
        
    def topic_base(self):
        "return base topic path allocated for this user: menshnet/client/<username>/#"
        if self.messenger.connected.is_set():
            return self.messenger.topic_base

    def transaction(self, topic, data, response_handler=None, timeout=15.0, error_handler=None):
        """
        Handle a transaction either in synchronous mode (blocking) or async. For async
        response must be a callable object. 
        """
        tx = MqttTransaction(self.messenger, topic, data)

        # set error handler, default is to just scream at the error log ;)
        if error_handler:
            tx.on_failure = error_handler
        
        if response_handler:
            tx.on_response = response_handler
        
        tx.begin()
        if not response_handler:
            tx.wait_for_completion(timeout)

        # reply will be none if in async mode (response_handler == None)
        return tx.reply 



    def connect(self, apiKey, user, **kwArgs):
        """
        Authenticate apiKey, enable mqtt communications to topics then
        connect to mqtt.        
        """
        self.messenger = Messenger(user)

        on_connected = kwArgs.get('on_connected')
        if on_connected and not callable(on_connected):
            raise ValueError("When specified on_connected must be a callable object/function")

        if os.environ.get("MENSHNET_UNITTEST","no") == "yes":
            class Fake_response:
                 def __init__(self):
                     self.content = json.dumps({
                         "name": os.environ["MENSHNET_UNITTEST_MQTT_USERNAME"],
                         "pwhash": os.environ["MENSHNET_UNITTEST_MQTT_PWHASH"]
                     }).encode('utf-8')
                     self.status_code = 200
            r = Fake_response()
            print("Unit test mode: %s" % str(vars(r)))
        else:
            r = requests.post(VALIDATE_API_URL, json={
                "apiKey": apiKey,
                "user": user
            })

        if r.status_code == 403:
            raise PermissionError(apiKey)
       
        elif r.status_code == 200:
            # validated, get the mqtt authentication information
            mqtt_auth = json.loads(r.content.decode())
            self.messenger.connect(mqtt_auth, on_connected)
            if not on_connected:
                # synchronous mode, block until connected.
                self.messenger.connected.wait()
        else:
            raise RuntimeError("HTTP %d Error while validating apiKey '%s'" % (r.status_code,r.content))
                  

class UnitTest(object):
    """
    Perform unit tests.
    """
    def __init__(self):
        pass

    def fake_tx_server(self, es):
        print("fake_tx_server")
        lock = threading.Event()
        def echo_reply(client, userdata, message):
            print("received message")
            data = message.payload.decode()
            m = json.loads(data)
            print("publish reply to %s" % m['reply_topic'])
            client.publish(m['reply_topic'],payload=json.dumps(m))
            print('fake_tx_server unlock')
            lock.set()
        print("subscribed to %s" % es.messenger.topic_base+"/echo")
        es.messenger.mqttc.subscribe(es.messenger.topic_base+"/echo")
        es.messenger.mqttc.message_callback_add(es.messenger.topic_base+"/echo",echo_reply) 
        lock.wait() 
        import time
        time.sleep(2)

    def fake_tx_client(self, es):
        r = es.transaction( es.messenger.topic_base+"/echo", {'hello':'world'})
        print("response " + str(r))

    def run(self):
        es = EventSystem()
        # connect synchronously 
        es.connect("dummy",os.environ["MENSHNET_UNITTEST_MQTT_USERNAME"])
        print("connected")
        cmd = sys.argv[1]
        method = getattr(self,cmd)
        method(es)        


if __name__ == '__main__':
    UnitTest().run()







     




"""Loopback MQTT broker stub: publishes are recorded, and inject() feeds
messages back through the subscriber callback on the next check_msg()."""
published = []
class MQTTClient:
    def __init__(self, cid, server, port=0, user=None, password=None, keepalive=0):
        self.cid = cid; self._cb = None; self._inbox = []; self.subs = []
        self.will = None
    def set_callback(self, cb): self._cb = cb
    def set_last_will(self, topic, msg, retain=False): self.will = (topic, msg)
    def connect(self, clean_session=True, timeout=None): self.timeout = timeout
    def disconnect(self): pass
    def subscribe(self, topic): self.subs.append(topic)
    def publish(self, topic, msg, retain=False):
        published.append((topic, msg, retain))
    def ping(self): pass
    def check_msg(self):
        while self._inbox:
            t, m = self._inbox.pop(0)
            self._cb(t, m)
    def inject(self, topic, payload): self._inbox.append((topic, payload))

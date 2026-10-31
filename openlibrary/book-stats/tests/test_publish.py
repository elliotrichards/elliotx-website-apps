import json

from publish import CACHE_CONTROL, STAGING_PREFIX, publish


class FakeBlob:
    def __init__(self, bucket, name):
        self.bucket, self.name = bucket, name
        self.cache_control = None

    def upload_from_string(self, body, content_type):
        self.bucket.objects[self.name] = {
            "body": body,
            "content_type": content_type,
            "cache_control": self.cache_control,
        }

    def delete(self):
        del self.bucket.objects[self.name]


class FakeBucket:
    def __init__(self):
        self.objects = {}

    def blob(self, name):
        return FakeBlob(self, name)

    def copy_blob(self, blob, destination, new_name):
        destination.objects[new_name] = dict(self.objects[blob.name])


def test_publish_stages_then_replaces_live_object():
    bucket = FakeBucket()
    size = publish(bucket, {"title": "Amélie"})

    assert list(bucket.objects) == ["books.json"]  # staging copy cleaned up
    live = bucket.objects["books.json"]
    assert live["content_type"] == "application/json"
    assert live["cache_control"] == CACHE_CONTROL
    assert json.loads(live["body"]) == {"title": "Amélie"}
    assert size == len(live["body"])
    assert not any(name.startswith(STAGING_PREFIX) for name in bucket.objects)

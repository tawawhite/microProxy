import time


class HttpRequest(object):
    def __init__(self,
                 version="",
                 method="",
                 path="",
                 headers=None,
                 body=b""):
        super(HttpRequest, self).__init__()
        self.timestamp = int(time.time())
        self.version = version
        self.method = method
        self.path = path
        self.body = body
        self.headers = HttpHeaders(headers)

    def serialize(self):
        json = {}
        json["timestamp"] = self.timestamp
        json["version"] = self.version
        json["method"] = self.method
        json["path"] = self.path
        json["body"] = self.body.encode("base64")
        json["headers"] = self.headers.get_list()
        return json


class HttpResponse(object):
    def __init__(self,
                 code="",
                 reason="",
                 version="",
                 headers=None,
                 body=b""):
        super(HttpResponse, self).__init__()
        self.timestamp = int(time.time())
        self.code = code
        self.reason = reason
        self.version = version
        self.body = body
        self.headers = HttpHeaders(headers)

    def serialize(self):
        json = {}
        json["timestamp"] = self.timestamp
        json["version"] = self.version
        json["code"] = self.code
        json["reason"] = self.reason
        json["body"] = self.body.encode("base64")
        json["headers"] = self.headers.get_list()
        return json


class HttpHeaders(object):
    def __init__(self, headers=None):
        headers = headers or []
        if isinstance(headers, dict):
            self.headers = headers.items()
        elif isinstance(headers, list):
            self.headers = headers
        else:
            raise ValueError("HttpHeaders not support with: " + str(type(headers)))

    def __getitem__(self, key):
        return [v for k, v in self.headers if k == key]

    def __setitem__(self, key, value):
        self.headers.append(key, value)

    def get_dict(self):
        return dict(self.headers)

    def get_list(self):
        return self.headers
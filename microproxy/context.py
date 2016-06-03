class Context(object):
    def __init__(self,
                 src_stream=None,
                 dest_stream=None,
                 scheme=None,
                 host=None,
                 port=None,
                 config=None):
        self.src_stream = src_stream
        self.dest_stream = dest_stream
        self.scheme = scheme
        self.host = host
        self.port = port
        self.config = config

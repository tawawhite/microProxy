# microProxy

**microProxy** is a http/https traffic interceptor which can help you debug with http/https based network traffic.
This project is highly inspired by [mitmproxy](https://github.com/mitmproxy/mitmproxy),
but with some different [architecture design](https://github.com/mike820324/microProxy/wiki/System-Architecture).


For more in depth information about our architecture design, please refer to the [wiki page](https://github.com/mike820324/microProxy/wiki).
> Note: We are still in a very earyly stage and as a result, the interface will be changed.

[![Build Status](https://travis-ci.org/mike820324/microProxy.svg?branch=master)](https://travis-ci.org/mike820324/microProxy)
[![Coverage Status](https://coveralls.io/repos/github/mike820324/microProxy/badge.svg?branch=master)](https://coveralls.io/github/mike820324/microProxy?branch=master) 

## Features:
- Proxy Mode:
  - SOCKS5 Proxy
  - Transparent Proxy(Linux Only)

- Protocol Support:
    - HTTP
    - HTTPS
    - HTTP2 (vis alpn, HTTP Upgrade is not supported)

- Flexible Viewer Design:
    - Viewer can connect to proxy with different machine.
    - Support multiple viewer connect to the same proxy instance.
    - Implement your own viewer by following the Viewer communication mechanism.

- Plugin System (still in very earyly stage):
    - Support external script to modify the Request/Response content. 

## Viewer Implementation List:
- Console Viwer: Simple console dump viewer.
- TUI Viwer: Terminal UI viewer which used [gviewer](https://github.com/chhsiao90/gviewer).
- [GUI Viewer](https://github.com/mike820324/microProxy-GUI): Graphic UI written in node.js and electron.

## System Requirement:
**microProxy** can run in both **Linux** and **MacOS**.

The python version we are using is version 2.7.10.

## Installation

This project is not update to pypi yet.
Therefore, to intall this project, please follow the following steps.

```bash
git clone https://github.com/mike820324/microProxy.git
cd microProxy

# install proxy dependency
pip install .

# install viewer dependency
pip install .[viewer]

# install dev dependency
pip install .[develop]
```

To run the proxy, simply type the following command.

```bash
# start proxy server
microproxy proxy --viewer-channel tcp://127.0.0.1:5581

# start tui-viewer
microproxy tui-viewer --viewer-channel tcp://127.0.0.1:5581

# start console-viewer
microproxy console-viewer --viewer-channel tcp://127.0.0.1:5581
```

For more information about command line options and configurations,
please refer to the [wiki page](https://github.com/mike820324/microProxy/wiki/Command-Line-Options-and-Config-Files).

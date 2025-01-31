"""
################################################################################
#
# SOAPpy - Cayce Ullman       (cayce@actzero.com)
#          Brian Matthews     (blm@actzero.com)
#          Gregory Warnes     (Gregory.R.Warnes@Pfizer.com)
#          Christopher Blunck (blunck@gst.com)
#
################################################################################
# Copyright (c) 2003, Pfizer
# Copyright (c) 2001, Cayce Ullman.
# Copyright (c) 2001, Brian Matthews.
#
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
# Redistributions of source code must retain the above copyright notice, this
# list of conditions and the following disclaimer.
#
# Redistributions in binary form must reproduce the above copyright notice,
# this list of conditions and the following disclaimer in the documentation
# and/or other materials provided with the distribution.
#
# Neither the name of actzero, inc. nor the names of its contributors may
# be used to endorse or promote products derived from this software without
# specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE REGENTS OR CONTRIBUTORS BE LIABLE FOR
# ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
# (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND
# ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
# SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#
################################################################################

"""


ident = '$Id: Client.py 1496 2010-03-04 23:46:17Z pooryorick $'

from .version import __version__
from io import StringIO

#import xml.sax
import urllib.request, urllib.parse, urllib.error
from .Types import *
import re
import base64
import socket, http.client
from http.client import HTTPConnection
import http.cookies

# SOAPpy-py3 modules
from .Errors      import *
from .Config      import Config
from .Parser      import parseSOAPRPC
from .SOAPBuilder import buildSOAP
from .Utilities   import *
from .Types       import faultType, simplify

import collections

################################################################################
# Client
################################################################################


def SOAPUserAgent():
    return "SOAPpy-py3 " + __version__ + " (pywebsvcs.sf.net)"


class HTTP:
    "Compatibility class with httplib.py from 1.5."

    _http_vsn = 10
    _http_vsn_str = 'HTTP/1.0'

    debuglevel = 0

    _connection_class = HTTPConnection

    def __init__(self, host='', port=None, strict=None):
        "Provide a default host, since the superclass requires one."

        # some joker passed 0 explicitly, meaning default port
        if port == 0:
            port = None

        # Note that we may pass an empty string as the host; this will raise
        # an error when we attempt to connect. Presumably, the client code
        # will call connect before then, with a proper host.
        self._setup(self._connection_class(host, port, strict))

    def _setup(self, conn):
        self._conn = conn

        # set up delegation to flesh out interface
        self.send = conn.send
        self.putrequest = conn.putrequest
        self.putheader = conn.putheader
        self.endheaders = conn.endheaders
        self.set_debuglevel = conn.set_debuglevel

        conn._http_vsn = self._http_vsn
        conn._http_vsn_str = self._http_vsn_str

        self.file = None

    def connect(self, host=None, port=None):
        "Accept arguments to set the host/port, since the superclass doesn't."

        if host is not None:
            (self._conn.host, self._conn.port) = self._conn._get_hostport(host, port)
        self._conn.connect()

    def getfile(self):
        "Provide a getfile, since the superclass' does not use this concept."
        return self.file

    def getreply(self, buffering=False):
        """Compat definition since superclass does not define it.

        Returns a tuple consisting of:
        - server status code (e.g. '200' if all goes well)
        - server "reason" corresponding to status code
        - any RFC822 headers in the response from the server
        """
        try:
            if not buffering:
                response = self._conn.getresponse()
            else:
                #only add this keyword if non-default for compatibility
                #with other connection classes
                response = self._conn.getresponse(buffering)
        except http.client.BadStatusLine as e:
            ### hmm. if getresponse() ever closes the socket on a bad request,
            ### then we are going to have problems with self.sock

            ### should we keep this behavior? do people use it?
            # keep the socket open (as a file), and return it
            self.file = self._conn.sock.makefile('rb', 0)

            # close our socket -- we want to restart after any protocol error
            self.close()

            self.headers = None
            return -1, e.line, None

        self.headers = response.msg
        self.file = StringIO(response.fp.read().decode('utf-8'))
        return response.status, response.reason, response.msg

    def close(self):
        self._conn.close()

        # note that self.file == response.fp, which gets closed by the
        # superclass. just clear the object ref here.
        ### hmm. messy. if status==-1, then self.file is owned by us.
        ### well... we aren't explicitly closing, but losing this ref will
        ### do it
        self.file = None


class SOAPAddress:
    def __init__(self, url, config = Config):
        proto, uri = urllib.parse.splittype(url)

        # apply some defaults
        if uri[0:2] != '//':
            if proto != None:
                uri = proto + ':' + uri

            uri = '//' + uri
            proto = 'http'

        host, path = urllib.parse.splithost(uri)

        try:
            int(host)
            host = 'localhost:' + host
        except:
            pass

        if not path:
            path = '/'

        if proto not in ('http', 'https', 'httpg'):
            raise IOError("unsupported SOAP protocol")
        if proto == 'httpg' and not config.GSIclient:
            raise AttributeError("GSI client not supported by this Python installation")
        if proto == 'https' and not config.SSLclient:
            raise AttributeError("SSL client not supported by this Python installation")

        self.user,host = urllib.parse.splituser(host)
        self.proto = proto
        self.host = host
        self.path = path

    def __str__(self):
        return "%(proto)s://%(host)s%(path)s" % self.__dict__

    __repr__ = __str__

class SOAPTimeoutError(socket.timeout):
    '''This exception is raised when a timeout occurs in SOAP operations'''
    pass

class HTTPConnectionWithTimeout(HTTPConnection):
    '''Extend HTTPConnection for timeout support'''

    def __init__(self, host, port=None, strict=None, timeout=None):
        HTTPConnection.__init__(self, host, port, strict)
        self._timeout = timeout

    def connect(self):
        HTTPConnection.connect(self)
        if self.sock and self._timeout:
            self.sock.settimeout(self._timeout) 


class HTTPWithTimeout(HTTP):

    _connection_class = HTTPConnectionWithTimeout

    def __init__(self, host='', port=None, strict=None, timeout=None):
        """Slight modification of superclass (httplib.HTTP) constructor.

        The only change is that arg ``timeout`` is also passed in the
        initialization of :attr:`_connection_class`.

        :param timeout: for the socket connection (seconds); None to disable
        :type timeout: float or None

        """
        if port == 0:
            port = None

        self._setup(self._connection_class(host, port, strict, timeout))

class HTTPTransport:
            

    def __init__(self):
        self.cookies = http.cookies.SimpleCookie();

    def getNS(self, original_namespace, data):
        """Extract the (possibly extended) namespace from the returned
        SOAP message."""

        if type(original_namespace) == StringType:
            pattern="xmlns:\w+=['\"](" + original_namespace + "[^'\"]*)['\"]"
            match = re.search(pattern, data)
            if match:
                return match.group(1)
            else:
                return original_namespace
        else:
            return original_namespace
    
    def __addcookies(self, r):
        '''Add cookies from self.cookies to request r
        '''
        for cname, morsel in list(self.cookies.items()):
            attrs = []
            value = morsel.get('version', '')
            if value != '' and value != '0':
                attrs.append('$Version=%s' % value)
            attrs.append('%s=%s' % (cname, morsel.coded_value))
            value = morsel.get('path')
            if value:
                attrs.append('$Path=%s' % value)
            value = morsel.get('domain')
            if value:
                attrs.append('$Domain=%s' % value)
            r.putheader('Cookie', "; ".join(attrs))
    
    def call(self, addr, data, namespace, soapaction = None, encoding = None,
        http_proxy = None, config = Config, timeout=None):

        if not isinstance(addr, SOAPAddress):
            addr = SOAPAddress(addr, config)

        # Build a request
        if http_proxy:
            real_addr = http_proxy
            real_path = addr.proto + "://" + addr.host + addr.path
        else:
            real_addr = addr.host
            real_path = addr.path

        if addr.proto == 'httpg':
            from pyGlobus.io import GSIHTTP
            r = GSIHTTP(real_addr, tcpAttr = config.tcpAttr)
        elif addr.proto == 'https':
            r = http.client.HTTPS(real_addr, key_file=config.SSL.key_file, cert_file=config.SSL.cert_file)
        else:
            r = HTTPWithTimeout(real_addr, timeout=timeout)

        r.putrequest("POST", real_path)

        r.putheader("Host", addr.host)
        r.putheader("User-agent", SOAPUserAgent())
        t = 'text/xml';
        if encoding != None:
            t += '; charset=%s' % encoding
        r.putheader("Content-type", t)
        r.putheader("Content-length", str(len(data)))
        self.__addcookies(r);
        
        # if user is not a user:passwd format
        #    we'll receive a failure from the server. . .I guess (??)
        if addr.user != None:
            val = base64.encodestring(urllib.parse.unquote_plus(addr.user))
            r.putheader('Authorization','Basic ' + val.replace('\012',''))

        # This fixes sending either "" or "None"
        if soapaction == None or len(soapaction) == 0:
            r.putheader("SOAPAction", "")
        else:
            r.putheader("SOAPAction", '"%s"' % soapaction)

        if config.dumpHeadersOut:
            s = 'Outgoing HTTP headers'
            debugHeader(s)
            print("POST %s %s" % (real_path, r._http_vsn_str))
            print("Host:", addr.host)
            print("User-agent: SOAPpy-py3 " + __version__ + " (http://pywebsvcs.sf.net)")
            print("Content-type:", t)
            print("Content-length:", len(data))
            print('SOAPAction: "%s"' % soapaction)
            debugFooter(s)

        r.endheaders()

        if config.dumpSOAPOut:
            s = 'Outgoing SOAP'
            debugHeader(s)
            print(data, end=' ')
            if data[-1] != '\n':
                print()
            debugFooter(s)

        # send the payload
        r.send(data)

        # read response line
        code, msg, headers = r.getreply()

        self.cookies = http.cookies.SimpleCookie();
        if headers:
            content_type = headers.get("content-type","text/xml")
            content_length = headers.get("Content-length")

            for cookie in headers.getallmatchingheaders("Set-Cookie"):
                self.cookies.load(cookie);

        else:
            content_type=None
            content_length=None

        # work around OC4J bug which does '<len>, <len>' for some reaason
        if content_length:
            comma=content_length.find(',')
            if comma>0:
                content_length = content_length[:comma]

        # attempt to extract integer message size
        try:
            message_len = int(content_length)
        except:
            message_len = -1
            
        f = r.getfile()
        if f is None:
            raise HTTPError(code, "Empty response from server\nCode: %s\nHeaders: %s" % (msg, headers))

        if message_len < 0:
            # Content-Length missing or invalid; just read the whole socket
            # This won't work with HTTP/1.1 chunked encoding
            data = f.read()
            message_len = len(data)
        else:
            data = f.read(message_len)

        if(config.debug):
            print("code=",code)
            print("msg=", msg)
            print("headers=", headers)
            print("content-type=", content_type)
            print("data=", data)
                
        if config.dumpHeadersIn:
            s = 'Incoming HTTP headers'
            debugHeader(s)
            if headers.headers:
                print("HTTP/1.? %d %s" % (code, msg))
                print("\n".join([x.strip() for x in headers.headers]))
            else:
                print("HTTP/0.9 %d %s" % (code, msg))
            debugFooter(s)

        def startswith(string, val):
            return string[0:len(val)] == val
        
        if code == 500 and not \
               ( startswith(content_type, "text/xml") and message_len > 0 ):
            raise HTTPError(code, msg)

        if config.dumpSOAPIn:
            s = 'Incoming SOAP'
            debugHeader(s)
            print(data, end=' ')
            if (len(data)>0) and (data[-1] != '\n'):
                print()
            debugFooter(s)

        if code not in (200, 500):
            raise HTTPError(code, msg)


        # get the new namespace
        if namespace is None:
            new_ns = None
        else:
            new_ns = self.getNS(namespace, data)
        
        # return response payload
        return data, new_ns

################################################################################
# SOAP Proxy
################################################################################
class SOAPProxy:
    def __init__(self, proxy, namespace = None, soapaction = None,
                 header = None, methodattrs = None, transport = HTTPTransport,
                 encoding = 'UTF-8', throw_faults = 1, unwrap_results = None,
                 http_proxy=None, config = Config, noroot = 0,
                 simplify_objects=None, timeout=None):

        # Test the encoding, raising an exception if it's not known
        if encoding != None:
            ''.encode(encoding)

        # get default values for unwrap_results and simplify_objects
        # from config
        if unwrap_results is None:
            self.unwrap_results=config.unwrap_results
        else:
            self.unwrap_results=unwrap_results

        if simplify_objects is None:
            self.simplify_objects=config.simplify_objects
        else:
            self.simplify_objects=simplify_objects

        self.proxy          = SOAPAddress(proxy, config)
        self.namespace      = namespace
        self.soapaction     = soapaction
        self.header         = header
        self.methodattrs    = methodattrs
        self.transport      = transport()
        self.encoding       = encoding
        self.throw_faults   = throw_faults
        self.http_proxy     = http_proxy
        self.config         = config
        self.noroot         = noroot
        self.timeout        = timeout

        # GSI Additions
        if hasattr(config, "channel_mode") and \
               hasattr(config, "delegation_mode"):
            self.channel_mode = config.channel_mode
            self.delegation_mode = config.delegation_mode
        #end GSI Additions
        
    def invoke(self, method, args):
        return self.__call(method, args, {})
        
    def __call(self, name, args, kw, ns = None, sa = None, hd = None,
        ma = None):

        ns = ns or self.namespace
        ma = ma or self.methodattrs

        if sa: # Get soapaction
            if type(sa) == TupleType:
                sa = sa[0]
        else:
            if self.soapaction:
                sa = self.soapaction
            else:
                sa = name
                
        if hd: # Get header
            if type(hd) == TupleType:
                hd = hd[0]
        else:
            hd = self.header

        hd = hd or self.header

        if ma: # Get methodattrs
            if type(ma) == TupleType: ma = ma[0]
        else:
            ma = self.methodattrs
        ma = ma or self.methodattrs

        m = buildSOAP(args = args, kw = kw, method = name, namespace = ns,
            header = hd, methodattrs = ma, encoding = self.encoding,
            config = self.config, noroot = self.noroot)


        call_retry = 0
        try:
            r, self.namespace = self.transport.call(self.proxy, m, ns, sa,
                                                    encoding = self.encoding,
                                                    http_proxy = self.http_proxy,
                                                    config = self.config,
                                                    timeout = self.timeout)

        except socket.timeout:
            raise SOAPTimeoutError

        except Exception as ex:
            #
            # Call failed.
            #
            # See if we have a fault handling vector installed in our
            # config. If we do, invoke it. If it returns a true value,
            # retry the call. 
            #
            # In any circumstance other than the fault handler returning
            # true, reraise the exception. This keeps the semantics of this
            # code the same as without the faultHandler code.
            #

            if hasattr(self.config, "faultHandler"):
                if isinstance(self.config.faultHandler, collections.Callable):
                    call_retry = self.config.faultHandler(self.proxy, ex)
                    if not call_retry:
                        raise
                else:
                    raise
            else:
                raise

        if call_retry:
            try:
                r, self.namespace = self.transport.call(self.proxy, m, ns, sa,
                                                        encoding = self.encoding,
                                                        http_proxy = self.http_proxy,
                                                        config = self.config,
                                                        timeout = self.timeout)
            except socket.timeout:
                raise SOAPTimeoutError
            

        p, attrs = parseSOAPRPC(r, attrs = 1)

        try:
            throw_struct = self.throw_faults and \
                isinstance (p, faultType)
        except:
            throw_struct = 0

        if throw_struct:
            if self.config.debug:
                print(p)
            raise p

        # If unwrap_results=1 and there is only element in the struct,
        # SOAPProxy will assume that this element is the result
        # and return it rather than the struct containing it.
        # Otherwise SOAPproxy will return the struct with all the
        # elements as attributes.
        if self.unwrap_results:
            try:
                count = 0
                for i in list(p.__dict__.keys()):
                    if i[0] != "_":  # don't count the private stuff
                        count += 1
                        t = getattr(p, i)
                if count == 1: # Only one piece of data, bubble it up
                    p = t 
            except:
                pass

        # Automatically simplfy SOAP complex types into the
        # corresponding python types. (structType --> dict,
        # arrayType --> array, etc.)
        if self.simplify_objects:
            p = simplify(p)

        if self.config.returnAllAttrs:
            return p, attrs
        return p

    def _callWithBody(self, body):
        return self.__call(None, body, {})

    def __getattr__(self, name):  # hook to catch method calls
        if name in ( '__del__', '__getinitargs__', '__getnewargs__',
           '__getstate__', '__setstate__', '__reduce__', '__reduce_ex__'):
            raise AttributeError(name)
        return self.__Method(self.__call, name, config = self.config)

    # To handle attribute weirdness
    class __Method:
        # Some magic to bind a SOAP method to an RPC server.
        # Supports "nested" methods (e.g. examples.getStateName) -- concept
        # borrowed from xmlrpc/soaplib -- www.pythonware.com
        # Altered (improved?) to let you inline namespaces on a per call
        # basis ala SOAP::LITE -- www.soaplite.com

        def __init__(self, call, name, ns = None, sa = None, hd = None,
            ma = None, config = Config):

            self.__call 	= call
            self.__name 	= name
            self.__ns   	= ns
            self.__sa   	= sa
            self.__hd   	= hd
            self.__ma           = ma
            self.__config       = config
            return

        def __call__(self, *args, **kw):
            if self.__name[0] == "_":
                if self.__name in ["__repr__","__str__"]:
                    return self.__repr__()
                else:
                    return self.__f_call(*args, **kw)
            else:
                return self.__r_call(*args, **kw)
                        
        def __getattr__(self, name):
            if name == '__del__':
                raise AttributeError(name)
            if self.__name[0] == "_":
                # Don't nest method if it is a directive
                return self.__class__(self.__call, name, self.__ns,
                    self.__sa, self.__hd, self.__ma)

            return self.__class__(self.__call, "%s.%s" % (self.__name, name),
                self.__ns, self.__sa, self.__hd, self.__ma)

        def __f_call(self, *args, **kw):
            if self.__name == "_ns": self.__ns = args
            elif self.__name == "_sa": self.__sa = args
            elif self.__name == "_hd": self.__hd = args
            elif self.__name == "_ma": self.__ma = args
            return self

        def __r_call(self, *args, **kw):
            return self.__call(self.__name, args, kw, self.__ns, self.__sa,
                self.__hd, self.__ma)

        def __repr__(self):
            return "<%s at %d>" % (self.__class__, id(self))

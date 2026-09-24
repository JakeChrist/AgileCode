"""A small, read-only MCP stdio example; not required for normal AgileCode use.

Run only as a configured MCP server: python examples/mcp_echo_server.py
Stdout is JSON-RPC only. No filesystem writes or external network access.
"""
import json
import os
import sys

def send(value):
    print(json.dumps(value),flush=True)

for line in sys.stdin:
    try:
        request=json.loads(line)
        method=request.get('method');identity=request.get('id')
        if identity is None:continue
        params=request.get('params',{})
        if method=='initialize':
            send({'jsonrpc':'2.0','id':'server-roots','method':'roots/list','params':{}})
            roots=json.loads(sys.stdin.readline())
            if roots.get('id')!='server-roots':raise ValueError('Expected roots response')
            result={'protocolVersion':params['protocolVersion'],'capabilities':{'tools':{},'resources':{}},'serverInfo':{'name':'AgileCode example echo','version':'1.0'}}
        elif method=='tools/list':
            if params.get('cursor')=='second':result={'tools':[]}
            else:result={'tools':[{'name':'echo','description':'Return supplied text and the configured working directory. No mutation.','inputSchema':{'type':'object','properties':{'text':{'type':'string'}},'required':['text'],'additionalProperties':False}}],'nextCursor':'second'}
        elif method=='tools/call':
            if params.get('name')!='echo':raise ValueError('Unknown tool')
            result={'content':[{'type':'text','text':params['arguments']['text']},{'type':'text','text':'cwd='+os.getcwd()}]}
        elif method=='resources/list':result={'resources':[{'uri':'memory://example','name':'Example instruction','mimeType':'text/plain'}]}
        elif method=='resources/read':
            if params.get('uri')!='memory://example':raise ValueError('Unknown resource')
            result={'contents':[{'uri':'memory://example','mimeType':'text/plain','text':'This is a read-only demonstration resource.'}]}
        elif method=='ping':result={}
        else:raise ValueError('Method not implemented')
        send({'jsonrpc':'2.0','id':identity,'result':result})
    except Exception as exc:
        send({'jsonrpc':'2.0','id':request.get('id'),'error':{'code':-32602,'message':str(exc)}})

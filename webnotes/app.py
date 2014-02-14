# Copyright (c) 2013, Web Notes Technologies Pvt. Ltd. and Contributors
# MIT License. See license.txt 

import sys, os
import json

from werkzeug.wrappers import Request, Response
from werkzeug.local import LocalManager
from werkzeug.wsgi import SharedDataMiddleware
from werkzeug.exceptions import HTTPException, NotFound
from werkzeug.contrib.profiler import ProfilerMiddleware

import mimetypes
import webnotes
import webnotes.handler
import webnotes.auth
import webnotes.api
import webnotes.website.render
from webnotes.utils import get_site_name

local_manager = LocalManager([webnotes.local])

_site = None

def handle_session_stopped():
	res = Response("""<html>
							<body style="background-color: #EEE;">
									<h3 style="width: 900px; background-color: #FFF; border: 2px solid #AAA; padding: 20px; font-family: Arial; margin: 20px auto">
											Updating.
											We will be back in a few moments...
									</h3>
							</body>
					</html>""")
	res.status_code = 503
	res.content_type = 'text/html'
	return res

@Request.application
def application(request):
	webnotes.local.request = request
	
	try:
		site = _site or get_site_name(request.host)
		webnotes.init(site=site)
		
		if not webnotes.local.conf:
			# site does not exist
			raise NotFound
		
		webnotes.local.form_dict = webnotes._dict({ k:v[0] if isinstance(v, (list, tuple)) else v \
			for k, v in (request.form or request.args).iteritems() })
				
		webnotes.local._response = Response()
		webnotes.http_request = webnotes.auth.HTTPRequest()

		if webnotes.local.form_dict.cmd:
			webnotes.handler.handle()
		elif webnotes.request.path.startswith("/api/"):
			webnotes.api.handle()
		elif webnotes.local.request.method in ('GET', 'HEAD'):
			webnotes.website.render.render(webnotes.request.path[1:])
		else:
			raise NotFound

	except HTTPException, e:
		return e
		
	except webnotes.AuthenticationError, e:
		webnotes._response.status_code=401
		
	except webnotes.SessionStopped, e:
		webnotes.local._response = handle_session_stopped()
		
	finally:
		if webnotes.conn:
			webnotes.conn.close()
	
	return webnotes.local._response

application = local_manager.make_middleware(application)

def serve(port=8000, profile=False, site=None):
	global application, _site
	_site = site
	
	from werkzeug.serving import run_simple

	if profile:
		application = ProfilerMiddleware(application)

	if not os.environ.get('NO_STATICS'):
		application = SharedDataMiddleware(application, {
			'/assets': 'assets',
		})
	
	if site:
		application = SharedDataMiddleware(application, {
			'/files': os.path.join(site, 'public', 'files')
		})

	run_simple('0.0.0.0', int(port), application, use_reloader=True, 
		use_debugger=True, use_evalex=True)

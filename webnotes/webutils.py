# Copyright (c) 2013, Web Notes Technologies Pvt. Ltd. and Contributors
# MIT License. See license.txt

from __future__ import unicode_literals
import webnotes
import json, os, time, re
from webnotes import _
import webnotes.utils
from webnotes.utils import get_request_site_address, encode, cint
from webnotes.model import default_fields
from webnotes.model.controller import DocListController
from urllib import quote

import mimetypes
from webnotes.website.doctype.website_sitemap.website_sitemap import add_to_sitemap, update_sitemap, remove_sitemap

# frequently used imports (used by other modules)
from webnotes.website.doctype.website_sitemap_permission.website_sitemap_permission \
	import get_access, clear_permissions

class PageNotFoundError(Exception): pass

def render(path):
	"""render html page"""
	path = resolve_path(path)
	
	try:
		data = render_page(path)
	except Exception:
		path = "error"
		data = render_page(path)
	
	data = set_content_type(data, path)
	webnotes._response.data = data
	webnotes._response.headers[b"Page Name"] = path.encode("utf-8")
	
def render_page(path):
	"""get page html"""
	cache_key = ("page_context:{}" if is_ajax() else "page:{}").format(path)

	out = None
	
	# try memcache
	if can_cache():
		out = webnotes.cache().get_value(cache_key)
		if out and is_ajax():
			out = out.get("data")
			
	if out:
		if hasattr(webnotes, "_response"):
			webnotes._response.headers[b"From Cache"] = True
		
		return out
	
	return build(path)
	
def build(path):
	if not webnotes.conn:
		webnotes.connect()
	
	build_method = (build_json if is_ajax() else build_page)
	try:
		return build_method(path)

	except webnotes.DoesNotExistError:
		hooks = webnotes.get_hooks()
		if hooks.website_catch_all:
			return build_method(hooks.website_catch_all[0])
		else:
			return build_method("404")
	
def build_json(path):
	return get_context(path).data
	
def build_page(path):
	context = get_context(path)
	
	html = webnotes.get_template(context.base_template_path).render(context)
	html = scrub_relative_urls(html)
		
	if can_cache(context.no_cache):
		webnotes.cache().set_value("page:" + path, html)
	
	return html

def get_context(path):
	context = None
	cache_key = "page_context:{}".format(path)
	
	# try from memcache
	if can_cache():
		context = webnotes.cache().get_value(cache_key)
	
	if not context:
		context = get_sitemap_options(path)

		# permission may be required for rendering
		context["access"] = get_access(context.pathname)

		context = build_context(context)

		if can_cache(context.no_cache):
			del context["access"]
			webnotes.cache().set_value(cache_key, context)

	else:
		context["access"] = get_access(context.pathname)
		
	context.update(context.data or {})
	
	# TODO private pages
	
	return context
	
def get_sitemap_options(path):
	sitemap_options = None
	cache_key = "sitemap_options:{}".format(path)

	if can_cache():
		sitemap_options = webnotes.cache().get_value(cache_key)

	if not sitemap_options:
		sitemap_options = build_sitemap_options(path)
		if can_cache(sitemap_options.no_cache):
			webnotes.cache().set_value(cache_key, sitemap_options)
	
	return sitemap_options
	
def build_sitemap_options(path):
	sitemap_options = webnotes.doc("Website Sitemap", path).fields
		
	sitemap_config = webnotes.doc("Website Sitemap Config", 
		sitemap_options.get("website_sitemap_config")).fields
	
	# get sitemap config fields too
	for fieldname in ("base_template_path", "template_path", "controller", "no_cache", "no_sitemap", 
		"page_name_field", "condition_field"):
		sitemap_options[fieldname] = sitemap_config.get(fieldname)
	
	sitemap_options.doctype = sitemap_options.ref_doctype
	sitemap_options.title = sitemap_options.page_title
	sitemap_options.pathname = sitemap_options.name
	
	# establish hierarchy
	sitemap_options.parents = webnotes.conn.sql("""select name, page_title from `tabWebsite Sitemap`
		where lft < %s and rgt > %s order by lft asc""", (sitemap_options.lft, sitemap_options.rgt), as_dict=True)

	sitemap_options.children = webnotes.conn.sql("""select * from `tabWebsite Sitemap`
		where parent_website_sitemap=%s 
			and public_read=1 order by idx asc, page_title asc""", (sitemap_options.name,), as_dict=True)
		
	# determine templates to be used
	if not sitemap_options.base_template_path:
		sitemap_options.base_template_path = "templates/base.html"
		
	return sitemap_options
	
def build_context(sitemap_options):
	"""get_context method of bean or module is supposed to render content templates and push it into context"""
	context = webnotes._dict(sitemap_options)
	context.update(get_website_settings())
	
	# provide bean
	if context.doctype and context.docname:
		context.bean = webnotes.bean(context.doctype, context.docname)
	
	if context.controller:
		module = webnotes.get_module(context.controller)
		if module and hasattr(module, "get_context"):
			context.update(module.get_context(context) or {})
			
	if context.get("base_template_path") != context.get("template_path") and not context.get("rendered"):
		context.data = render_blocks(context)
	
	# remove bean, as it is not pickle friendly and its purpose is over
	if context.bean:
		del context["bean"]
	
	return context
	
def can_cache(no_cache=False):
	return not (webnotes.conf.disable_website_cache or no_cache)
	
def get_home_page():
	home_page = webnotes.cache().get_value("home_page", \
		lambda:  (webnotes.get_hooks("home_page") \
			or [webnotes.conn.get_value("Website Settings", None, "home_page") \
			or "login"])[0])

	print home_page

	return home_page
	
def get_website_settings():
	# TODO Cache this
	hooks = webnotes.get_hooks()
	
	all_top_items = webnotes.conn.sql("""\
		select * from `tabTop Bar Item`
		where parent='Website Settings' and parentfield='top_bar_items'
		order by idx asc""", as_dict=1)
	
	top_items = [d for d in all_top_items if not d['parent_label']]
	
	# attach child items to top bar
	for d in all_top_items:
		if d['parent_label']:
			for t in top_items:
				if t['label']==d['parent_label']:
					if not 'child_items' in t:
						t['child_items'] = []
					t['child_items'].append(d)
					break
					
	context = webnotes._dict({
		'top_bar_items': top_items,
		'footer_items': webnotes.conn.sql("""\
			select * from `tabTop Bar Item`
			where parent='Website Settings' and parentfield='footer_items'
			order by idx asc""", as_dict=1),
		"post_login": [
			{"label": "Reset Password", "url": "update-password", "icon": "icon-key"},
			{"label": "Logout", "url": "?cmd=web_logout", "icon": "icon-signout"}
		]
	})
		
	settings = webnotes.doc("Website Settings", "Website Settings")
	for k in ["banner_html", "brand_html", "copyright", "twitter_share_via",
		"favicon", "facebook_share", "google_plus_one", "twitter_share", "linked_in_share",
		"disable_signup"]:
		if k in settings.fields:
			context[k] = settings.fields.get(k)
			
	if settings.address:
		context["footer_address"] = settings.address

	for k in ["facebook_share", "google_plus_one", "twitter_share", "linked_in_share",
		"disable_signup"]:
		context[k] = cint(context.get(k) or 0)
	
	context.url = quote(str(get_request_site_address(full_address=True)), safe="/:")
	context.encoded_title = quote(encode(context.title or ""), str(""))
	
	for update_website_context in hooks.update_website_context or []:
		webnotes.get_attr(update_website_context)(context)
		
	context.web_include_js = hooks.web_include_js or []
	context.web_include_css = hooks.web_include_css or []
	
	return context
	
def is_ajax():
	return webnotes.get_request_header("X-Requested-With")=="XMLHttpRequest"
	
def resolve_path(path):
	if not path:
		path = "index"
			
	if path.endswith('.html'):
		path = path[:-5]
		
	if path == "index":
		path = get_home_page()
		
	return path

def set_content_type(data, path):
	if isinstance(data, dict):
		webnotes._response.headers[b"Content-Type"] = b"application/json; charset: utf-8"
		data = json.dumps(data)
		return data
	
	webnotes._response.headers[b"Content-Type"] = b"text/html; charset: utf-8"
	
	if "." in path and not path.endswith(".html"):
		content_type, encoding = mimetypes.guess_type(path)
		webnotes._response.headers[b"Content-Type"] = content_type.encode("utf-8")
	
	return data

def clear_cache(path=None):
	cache = webnotes.cache()
	
	if path:
		delete_page_cache(path)
		
	else:
		for p in webnotes.conn.sql_list("""select name from `tabWebsite Sitemap`"""):
			if p is not None:
				delete_page_cache(p)
		
		cache.delete_value("home_page")
		clear_permissions()
	
	for method in webnotes.get_hooks("website_clear_cache"):
		webnotes.get_attr(method)(path)

def delete_page_cache(path):
	cache = webnotes.cache()
	cache.delete_value("page:" + path)
	cache.delete_value("page_context:" + path)
	cache.delete_value("sitemap_options:" + path)
			
def is_signup_enabled():
	if getattr(webnotes.local, "is_signup_enabled", None) is None:
		webnotes.local.is_signup_enabled = True
		if webnotes.utils.cint(webnotes.conn.get_value("Website Settings", 
			"Website Settings", "disable_signup")):
				webnotes.local.is_signup_enabled = False
		
	return webnotes.local.is_signup_enabled
	
def call_website_generator(bean, method, *args, **kwargs):
	getattr(WebsiteGenerator(bean.doc, bean.doclist), method)(*args, **kwargs)
	
class WebsiteGenerator(DocListController):
	def autoname(self):
		from webnotes.webutils import cleanup_page_name
		self.doc.name = cleanup_page_name(self.get_page_title())

	def set_page_name(self):
		"""set page name based on parent page_name and title"""
		page_name = cleanup_page_name(self.get_page_title())

		if self.doc.is_new():
			self.doc.fields[self._website_config.page_name_field] = page_name
		else:
			webnotes.conn.set(self.doc, self._website_config.page_name_field, page_name)

	def setup_generator(self):
		self._website_config = webnotes.conn.get_values("Website Sitemap Config", 
			{"ref_doctype": self.doc.doctype}, "*")[0]

	def on_update(self):
		self.update_sitemap()
		
	def after_rename(self, olddn, newdn, merge):
		webnotes.conn.sql("""update `tabWebsite Sitemap`
			set docname=%s where ref_doctype=%s and docname=%s""", (newdn, self.doc.doctype, olddn))
		
		if merge:
			self.setup_generator()
			remove_sitemap(ref_doctype=self.doc.doctype, docname=olddn)
		
	def on_trash(self):
		self.setup_generator()
		remove_sitemap(ref_doctype=self.doc.doctype, docname=self.doc.name)
		
	def update_sitemap(self):
		self.setup_generator()
		
		if self._website_config.condition_field and \
			not self.doc.fields.get(self._website_config.condition_field):
			# condition field failed, remove and return!
			remove_sitemap(ref_doctype=self.doc.doctype, docname=self.doc.name)
			return
				
		self.add_or_update_sitemap()
		
	def add_or_update_sitemap(self):
		page_name = self.get_page_name()
		
		existing_site_map = webnotes.conn.get_value("Website Sitemap", {"ref_doctype": self.doc.doctype,
			"docname": self.doc.name})
			
		opts = webnotes._dict({
			"page_or_generator": "Generator",
			"ref_doctype":self.doc.doctype, 
			"docname": self.doc.name,
			"page_name": page_name,
			"link_name": self._website_config.name,
			"lastmod": webnotes.utils.get_datetime(self.doc.modified).strftime("%Y-%m-%d"),
			"parent_website_sitemap": self.doc.parent_website_sitemap,
			"page_title": self.get_page_title()
		})

		self.update_permissions(opts)

		if existing_site_map:
			update_sitemap(existing_site_map, opts)
		else:
			add_to_sitemap(opts)
	
	def update_permissions(self, opts):
		if self.meta.get_field("public_read"):
			opts.public_read = self.doc.public_read
			opts.public_write = self.doc.public_write
		else:
			opts.public_read = 1
	
	def get_page_name(self):
		if not self._get_page_name():
			self.set_page_name()
			
		return self._get_page_name()
		
	def _get_page_name(self):
		return self.doc.fields.get(self._website_config.page_name_field)
		
	def get_page_title(self):
		return self.doc.title or (self.doc.name.replace("-", " ").replace("_", " ").title())
		
def cleanup_page_name(title):
	"""make page name from title"""
	import re
	name = title.lower()
	name = re.sub('[~!@#$%^&*+()<>,."\'\?]', '', name)
	name = re.sub('[:/]', '-', name)

	name = '-'.join(name.split())

	# replace repeating hyphens
	name = re.sub(r"(-)\1+", r"\1", name)
	
	return name

def get_hex_shade(color, percent):
	def p(c):
		v = int(c, 16) + int(int('ff', 16) * (float(percent)/100))
		if v < 0: 
			v=0
		if v > 255: 
			v=255
		h = hex(v)[2:]
		if len(h) < 2:
			h = "0" + h
		return h
		
	r, g, b = color[0:2], color[2:4], color[4:6]
	
	avg = (float(int(r, 16) + int(g, 16) + int(b, 16)) / 3)
	# switch dark and light shades
	if avg > 128:
		percent = -percent

	# stronger diff for darker shades
	if percent < 25 and avg < 64:
		percent = percent * 2
	
	return p(r) + p(g) + p(b)

def render_blocks(context):
	"""returns a dict of block name and its rendered content"""
	from jinja2.utils import concat
	out = {}
	
	template = webnotes.get_template(context["template_path"])
		
	# required as per low level API
	context = template.new_context(context)
	
	# render each block individually
	for block, render in template.blocks.items():
		out[block] = scrub_relative_urls(concat(render(context)))

	return out

def scrub_relative_urls(html):
	"""prepend a slash before a relative url"""
	return re.sub("""(src|href)[^\w'"]*['"](?!http|ftp|/|#)([^'" >]+)['"]""", '\g<1> = "/\g<2>"', html)

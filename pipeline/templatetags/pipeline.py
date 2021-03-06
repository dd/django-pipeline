from __future__ import unicode_literals

import logging
import subprocess

from django.contrib.staticfiles.storage import staticfiles_storage

from django import template
from django.template.base import Context, VariableDoesNotExist
from django.template.loader import render_to_string
from django.template import Template
from django.utils.safestring import mark_safe
from django.contrib.staticfiles import finders

from ..collector import default_collector
from ..conf import settings
from ..exceptions import CompilerError
from ..packager import Packager, PackageNotFound
from ..utils import guess_type

logger = logging.getLogger(__name__)

register = template.Library()


class PipelineMixin(object):
    request = None
    _request_var = None

    inline = False
    inline_var = None

    def __init__(self, name, inline=False):
        self.name = name
        self.inline_var = inline

    @property
    def request_var(self):
        if not self._request_var:
            self._request_var = template.Variable('request')
        return self._request_var

    def package_for(self, package_name, package_type):
        package = {
            'js': getattr(settings, 'JAVASCRIPT', {}).get(package_name, {}),
            'css': getattr(settings, 'STYLESHEETS', {}).get(package_name, {}),
        }[package_type]

        if package:
            package = {package_name: package}

        packager = {
            'js': Packager(css_packages={}, js_packages=package),
            'css': Packager(css_packages=package, js_packages={}),
        }[package_type]

        return packager.package_for(package_type, package_name)

    def render(self, context):
        try:
            self.request = self.request_var.resolve(context)
        except VariableDoesNotExist:
            pass

        if self.inline_var:
            try:
                self.inline = template.Variable(self.inline_var).resolve(context)
            except VariableDoesNotExist:
                if self.inline_var == 'inline':
                    self.inline = True

    def render_compressed(self, package, package_name, package_type):
        """Render HTML for the package.

        If ``PIPELINE_ENABLED`` is ``True``, this will render the package's
        output file (using :py:meth:`render_compressed_output`). Otherwise,
        this will render the package's source files (using
        :py:meth:`render_compressed_sources`).

        Subclasses can override this method to provide custom behavior for
        determining what to render.
        """
        if settings.PIPELINE_ENABLED:
            return self.render_compressed_output(package, package_name,
                                                 package_type)
        else:
            return self.render_compressed_sources(package, package_name,
                                                  package_type)

    def render_compressed_output(self, package, package_name, package_type):
        """Render HTML for using the package's output file.

        Subclasses can override this method to provide custom behavior for
        rendering the output file.
        """
        method = getattr(self, 'render_{0}'.format(package_type))

        return method(package, package.output_filename)

    def render_compressed_sources(self, package, package_name, package_type):
        """Render HTML for using the package's list of source files.

        Each source file will first be collected, if
        ``PIPELINE_COLLECTOR_ENABLED`` is ``True``.

        If there are any errors compiling any of the source files, an
        ``SHOW_ERRORS_INLINE`` is ``True``, those errors will be shown at
        the top of the page.

        Subclasses can override this method to provide custom behavior for
        rendering the source files.
        """
        if settings.PIPELINE_COLLECTOR_ENABLED:
            default_collector.collect(self.request)

        packager = Packager()
        method = getattr(self, 'render_individual_{0}'.format(package_type))

        try:
            paths = packager.compile(package.paths)
        except CompilerError as e:
            if settings.SHOW_ERRORS_INLINE:
                method = getattr(self, 'render_error_{0}'.format(
                    package_type))

                return method(package_name, e)
            else:
                raise

        templates = packager.pack_templates(package)

        return method(package, paths, templates=templates)

    def render_inline(self, package, source, package_type):
        context = package.extra_context
        context.update({
            'source': source
        })
        return render_to_string("pipeline/inline_%s.html" % package_type, context)

    def render_error(self, package_type, package_name, e):
        return render_to_string('pipeline/compile_error.html', {
            'package_type': package_type,
            'package_name': package_name,
            'command': subprocess.list2cmdline(e.command),
            'errors': e.error_output,
        })


class StylesheetNode(PipelineMixin, template.Node):

    def render(self, context):
        super(StylesheetNode, self).render(context)
        package_name = template.Variable(self.name).resolve(context)

        try:
            package = self.package_for(package_name, 'css')
        except PackageNotFound:
            logger.warn("Package %r is unknown. Check PIPELINE['STYLESHEETS'] in your settings.", package_name)
            return ''  # fail silently, do not return anything if an invalid group is specified

        return self.render_compressed(package, package_name, 'css')

    def render_css(self, package, path):
        if self.inline:
            src = ""
            with open(staticfiles_storage.path(path), "r") as resourse:
                src = resourse.read()
                src = src.replace('../', staticfiles_storage.url('/'.join(path.split('/')[:-2])+'/'))
            if src:
                return self.render_inline(package, src, 'css')

        template_name = package.template_name or "pipeline/css.html"
        context = package.extra_context
        context.update({
            'type': guess_type(path, 'text/css'),
            'url': mark_safe(staticfiles_storage.url(path))
        })
        return render_to_string(template_name, context)

    def render_individual_css(self, package, paths, **kwargs):
        tags = [self.render_css(package, path) for path in paths]
        return '\n'.join(tags)

    def render_error_css(self, package_name, e):
        return super(StylesheetNode, self).render_error(
            'CSS', package_name, e)


class JavascriptNode(PipelineMixin, template.Node):

    def render(self, context):
        super(JavascriptNode, self).render(context)
        package_name = template.Variable(self.name).resolve(context)

        try:
            package = self.package_for(package_name, 'js')
        except PackageNotFound:
            logger.warn("Package %r is unknown. Check PIPELINE['JAVASCRIPT'] in your settings.", package_name)
            return ''  # fail silently, do not return anything if an invalid group is specified

        return self.render_compressed(package, package_name, 'js')

    def render_js(self, package, path):
        template_name = package.template_name or "pipeline/js.html"
        context = package.extra_context
        context.update({
            'type': guess_type(path, 'text/javascript'),
            'url': mark_safe(staticfiles_storage.url(path))
        })

        if self.inline:
            src = ""
            with open(staticfiles_storage.path(path), "r") as resourse:
                src = resourse.read()
            if src:
                return self.render_inline(package, src, 'js')

        return render_to_string(template_name, context)

    def render_individual_js(self, package, paths, templates=None):
        tags = [self.render_js(package, js) for js in paths]
        if templates:
            tags.append(self.render_inline(package, templates, 'js'))
        return '\n'.join(tags)

    def render_error_js(self, package_name, e):
        return super(JavascriptNode, self).render_error(
            'JavaScript', package_name, e)


@register.tag
def stylesheet(parser, token):
    args = token.split_contents()
    if len(args) < 2:
        raise template.TemplateSyntaxError('%r requires first argument: the name of a group in the PIPELINE.STYLESHEETS setting' % args[0])
    return StylesheetNode(*args[1:])


@register.tag
def javascript(parser, token):
    args = token.split_contents()
    if len(args) < 2:
        raise template.TemplateSyntaxError('%r requires first argument: the name of a group in the PIPELINE.JAVASVRIPT setting' % args[0])
    return JavascriptNode(*args[1:])

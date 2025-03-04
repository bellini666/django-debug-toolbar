import inspect
import os.path
import re
import sys
from importlib import import_module
from pprint import pformat

import django
from django.core.exceptions import ImproperlyConfigured
from django.template import Node
from django.utils.html import format_html
from django.utils.safestring import mark_safe

from debug_toolbar import settings as dt_settings

try:
    import threading
except ImportError:
    threading = None


# Figure out some paths
django_path = os.path.realpath(os.path.dirname(django.__file__))


def get_module_path(module_name):
    try:
        module = import_module(module_name)
    except ImportError as e:
        raise ImproperlyConfigured(f"Error importing HIDE_IN_STACKTRACES: {e}")
    else:
        source_path = inspect.getsourcefile(module)
        if source_path.endswith("__init__.py"):
            source_path = os.path.dirname(source_path)
        return os.path.realpath(source_path)


hidden_paths = [
    get_module_path(module_name)
    for module_name in dt_settings.get_config()["HIDE_IN_STACKTRACES"]
]


def omit_path(path):
    return any(path.startswith(hidden_path) for hidden_path in hidden_paths)


def tidy_stacktrace(stack):
    """
    Clean up stacktrace and remove all entries that:
    1. Are part of Django (except contrib apps)
    2. Are part of socketserver (used by Django's dev server)
    3. Are the last entry (which is part of our stacktracing code)

    ``stack`` should be a list of frame tuples from ``inspect.stack()``
    """
    trace = []
    for frame, path, line_no, func_name, text in (f[:5] for f in stack):
        if omit_path(os.path.realpath(path)):
            continue
        text = "".join(text).strip() if text else ""
        frame_locals = (
            frame.f_locals
            if dt_settings.get_config()["ENABLE_STACKTRACES_LOCALS"]
            else None
        )
        trace.append((path, line_no, func_name, text, frame_locals))
    return trace


def render_stacktrace(trace):
    show_locals = dt_settings.get_config()["ENABLE_STACKTRACES_LOCALS"]
    html = ""
    for abspath, lineno, func, code, locals_ in trace:
        directory, filename = abspath.rsplit(os.path.sep, 1)
        html += format_html(
            (
                '<span class="djdt-path">{}/</span>'
                + '<span class="djdt-file">{}</span> in'
                + ' <span class="djdt-func">{}</span>'
                + '(<span class="djdt-lineno">{}</span>)\n'
                + '  <span class="djdt-code">{}</span>\n'
            ),
            directory,
            filename,
            func,
            lineno,
            code,
        )
        if show_locals:
            html += format_html(
                '  <pre class="djdt-locals">{}</pre>\n',
                pformat(locals_),
            )
        html += "\n"
    return mark_safe(html)


def get_template_info():
    template_info = None
    cur_frame = sys._getframe().f_back
    try:
        while cur_frame is not None:
            in_utils_module = cur_frame.f_code.co_filename.endswith(
                "/debug_toolbar/utils.py"
            )
            is_get_template_context = (
                cur_frame.f_code.co_name == get_template_context.__name__
            )
            if in_utils_module and is_get_template_context:
                # If the method in the stack trace is this one
                # then break from the loop as it's being check recursively.
                break
            elif cur_frame.f_code.co_name == "render":
                node = cur_frame.f_locals["self"]
                context = cur_frame.f_locals["context"]
                if isinstance(node, Node):
                    template_info = get_template_context(node, context)
                    break
            cur_frame = cur_frame.f_back
    except Exception:
        pass
    del cur_frame
    return template_info


def get_template_context(node, context, context_lines=3):
    line, source_lines, name = get_template_source_from_exception_info(node, context)
    debug_context = []
    start = max(1, line - context_lines)
    end = line + 1 + context_lines

    for line_num, content in source_lines:
        if start <= line_num <= end:
            debug_context.append(
                {"num": line_num, "content": content, "highlight": (line_num == line)}
            )

    return {"name": name, "context": debug_context}


def get_template_source_from_exception_info(node, context):
    if context.template.origin == node.origin:
        exception_info = context.template.get_exception_info(
            Exception("DDT"), node.token
        )
    else:
        exception_info = context.render_context.template.get_exception_info(
            Exception("DDT"), node.token
        )
    line = exception_info["line"]
    source_lines = exception_info["source_lines"]
    name = exception_info["name"]
    return line, source_lines, name


def get_name_from_obj(obj):
    if hasattr(obj, "__name__"):
        name = obj.__name__
    else:
        name = obj.__class__.__name__

    if hasattr(obj, "__module__"):
        module = obj.__module__
        name = f"{module}.{name}"

    return name


def getframeinfo(frame, context=1):
    """
    Get information about a frame or traceback object.

    A tuple of five things is returned: the filename, the line number of
    the current line, the function name, a list of lines of context from
    the source code, and the index of the current line within that list.
    The optional second argument specifies the number of lines of context
    to return, which are centered around the current line.

    This originally comes from ``inspect`` but is modified to handle issues
    with ``findsource()``.
    """
    if inspect.istraceback(frame):
        lineno = frame.tb_lineno
        frame = frame.tb_frame
    else:
        lineno = frame.f_lineno
    if not inspect.isframe(frame):
        raise TypeError("arg is not a frame or traceback object")

    filename = inspect.getsourcefile(frame) or inspect.getfile(frame)
    if context > 0:
        start = lineno - 1 - context // 2
        try:
            lines, lnum = inspect.findsource(frame)
        except Exception:  # findsource raises platform-dependant exceptions
            first_lines = lines = index = None
        else:
            start = max(start, 1)
            start = max(0, min(start, len(lines) - context))
            first_lines = lines[:2]
            lines = lines[start : (start + context)]
            index = lineno - 1 - start
    else:
        first_lines = lines = index = None

    # Code taken from Django's ExceptionReporter._get_lines_from_file
    if first_lines and isinstance(first_lines[0], bytes):
        encoding = "ascii"
        for line in first_lines[:2]:
            # File coding may be specified. Match pattern from PEP-263
            # (https://www.python.org/dev/peps/pep-0263/)
            match = re.search(br"coding[:=]\s*([-\w.]+)", line)
            if match:
                encoding = match.group(1).decode("ascii")
                break
        lines = [line.decode(encoding, "replace") for line in lines]

    if hasattr(inspect, "Traceback"):
        return inspect.Traceback(filename, lineno, frame.f_code.co_name, lines, index)
    else:
        return (filename, lineno, frame.f_code.co_name, lines, index)


def get_sorted_request_variable(variable):
    """
    Get a sorted list of variables from the request data.
    """
    if isinstance(variable, dict):
        return [(k, variable.get(k)) for k in sorted(variable)]
    else:
        return [(k, variable.getlist(k)) for k in sorted(variable)]


def get_stack(context=1):
    """
    Get a list of records for a frame and all higher (calling) frames.

    Each record contains a frame object, filename, line number, function
    name, a list of lines of context, and index within the context.

    Modified version of ``inspect.stack()`` which calls our own ``getframeinfo()``
    """
    frame = sys._getframe(1)
    framelist = []
    while frame:
        framelist.append((frame,) + getframeinfo(frame, context))
        frame = frame.f_back
    return framelist


class ThreadCollector:
    def __init__(self):
        if threading is None:
            raise NotImplementedError(
                "threading module is not available, "
                "this panel cannot be used without it"
            )
        self.collections = {}  # a dictionary that maps threads to collections

    def get_collection(self, thread=None):
        """
        Returns a list of collected items for the provided thread, of if none
        is provided, returns a list for the current thread.
        """
        if thread is None:
            thread = threading.current_thread()
        if thread not in self.collections:
            self.collections[thread] = []
        return self.collections[thread]

    def clear_collection(self, thread=None):
        if thread is None:
            thread = threading.current_thread()
        if thread in self.collections:
            del self.collections[thread]

    def collect(self, item, thread=None):
        self.get_collection(thread).append(item)

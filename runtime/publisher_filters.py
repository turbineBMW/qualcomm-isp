# SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
# SPDX-License-Identifier: MIT
"""Optional GStreamer filters for the publisher, from drop-in files.

Packages such as qualcomm-isp-autoframe insert processing between the
publisher's frame queue (NV12 at the ISP's native size) and its scaler by
dropping a JSON file into /etc/qualcomm-isp/publisher.d/:

    {
      "nodes": ["sp11-ipe-preview"],
      "filter": "ispautoframe control-file={config_home}/qualcomm-isp/autoframe.conf ! capsfilter caps=video/x-raw,width=1920,height=1080",
      "formats": [[1920, 1080], [1280, 720], [640, 360]],
      "enabled": true
    }

`nodes` lists the publisher node names the filter applies to (absent: every
publisher). `filter` is a gst-launch description (a trailing caps string
needs `capsfilter caps=...`: bare caps only parse between two elements); it may use {config_home},
{home} and {runtime_dir}, resolved in the publisher's own environment, which
is the user's session. `formats` replaces the sizes offered to PipeWire peers
(the filter's output size first); without it the native list stays. Files are
applied in name order. Removing the package removes the file and the publisher
is back to its plain pipeline.
"""
import json
import os
from pathlib import Path

DEFAULT_DIRECTORY = '/etc/qualcomm-isp/publisher.d'


class Filters:
    def __init__(self, descriptions=(), formats=None, files=()):
        self.descriptions = list(descriptions)
        self.formats = formats
        self.files = list(files)

    def __bool__(self):
        return bool(self.descriptions)


def _placeholders(environ):
    home = environ.get('HOME') or str(Path.home())
    return dict(home=home,
                config_home=environ.get('XDG_CONFIG_HOME') or str(Path(home) / '.config'),
                runtime_dir=environ.get('XDG_RUNTIME_DIR') or '/tmp')


def _formats(path, value):
    if not isinstance(value, list) or not value:
        raise ValueError(f'{path}: "formats" must be a non-empty list of [width, height]')
    result = []
    for item in value:
        if (not isinstance(item, list) or len(item) != 2 or not all(isinstance(v, int) and not isinstance(v, bool) for v in item)
                or min(item) < 2 or any(v % 2 for v in item)):
            raise ValueError(f'{path}: "formats" entries must be [width, height] even integers')
        result.append((item[0], item[1]))
    return result


def load(node_name, directory=None, environ=None):
    """The filters for one publisher node, in application order."""
    environ = os.environ if environ is None else environ
    directory = Path(directory or environ.get('QUALCOMM_ISP_PUBLISHER_D', DEFAULT_DIRECTORY))
    placeholders = _placeholders(environ)
    filters = Filters()
    if not directory.is_dir():
        return filters
    for path in sorted(directory.glob('*.json')):
        spec = json.loads(path.read_text())
        if not isinstance(spec, dict):
            raise ValueError(f'{path}: expected a JSON object')
        if spec.get('enabled', True) is False:
            continue
        nodes = spec.get('nodes')
        if nodes is not None and (not isinstance(nodes, list) or node_name not in nodes):
            continue
        description = spec.get('filter')
        if not isinstance(description, str) or not description.strip():
            raise ValueError(f'{path}: "filter" must be a GStreamer pipeline description')
        try:
            description = description.format(**placeholders)
        except (KeyError, IndexError, ValueError) as exc:
            raise ValueError(f'{path}: unknown placeholder in "filter": {exc}') from None
        filters.descriptions.append(description.strip())
        filters.files.append(str(path))
        if 'formats' in spec:
            filters.formats = _formats(path, spec['formats'])
    return filters

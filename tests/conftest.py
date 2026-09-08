"""Keep pytest from importing the integration package.

``__init__.py`` needs a Home Assistant runtime; ``logic.py`` does not. The
tests load ``logic`` directly by path.
"""

collect_ignore = ["../custom_components/name_curator/__init__.py"]

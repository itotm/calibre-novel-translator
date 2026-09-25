import unittest
from importlib import import_module


class TestImports(unittest.TestCase):
    """The GUI modules are not exercised by the suite, so at least make
    sure every one of them still imports: a name removed from a module
    that another one still asks for would otherwise only be found when
    calibre loads the plugin."""

    def test_every_module_imports(self):
        package = __name__.rsplit('.', 1)[0].rsplit('.', 1)[0]
        for name in ('ui', 'setting', 'novel', 'text_view', 'cache', 'about',
                     'components', 'engines', 'lib.conversion'):
            with self.subTest(module=name):
                import_module('%s.%s' % (package, name))

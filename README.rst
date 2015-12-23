wordpress-to-puput
==================

Import your Wordpress blog data into Puput.

Usage
-----
1. Install wordpress-to-puput package and its dependencies :code:`pip install wordpress-to-puput`
2. Add :code:`wordpress2puput` to your :code:`INSTALLED_APPS` in :code:`settings.py` file.
3. Run the management command::

    python manage.py wp2puput path_to_wordpress_export.xml

You can optionally pass the slug and the title of the blog to the importer::

    python manage.py wp2puput path_to_wordpress_export.xml --slug=blog --title="Puput blog"


# -*- coding: utf-8 -*-
"""WordPress to puput command module"""
import lxml.html
from pip.backwardcompat import raw_input
import pytz

from datetime import datetime
from optparse import make_option
from xml.etree import ElementTree as ET

try:
    from urllib.request import urlopen
except ImportError:  # Python 2
    from urllib2 import urlopen

from django.conf import settings
from django.utils import timezone
from django.core.files import File
from django.utils.text import Truncator
from django.utils.html import strip_tags
from django.contrib.sites.models import Site
from django.db.utils import IntegrityError
from django.contrib.auth.models import User
from django.template.defaultfilters import slugify
from django.core.management.base import CommandError
from django.core.management.base import LabelCommand
from django.core.files.temp import NamedTemporaryFile

from wagtail.wagtailcore.models import Page
from wagtail.wagtailimages.models import Image as WagtailImage

from puput.models import BlogPage, EntryPage, TagEntryPage as PuputTagEntryPage, Tag as PuputTag, \
    Category as PuputCategory, CategoryEntryPage as PuputCategoryEntryPage

WP_NS = 'http://wordpress.org/export/%s/'


class Command(LabelCommand):
    """
    Command object for importing a WordPress blog
    into Puput via a WordPress eXtended RSS (WXR) file.
    """
    help = 'Import a Wordpress blog into Zinnia.'
    label = 'WXR file'
    args = 'wordpress.xml'

    SITE = Site.objects.get_current()

    def add_arguments(self, parser):
        parser.add_argument('--slug', default='blog', help="Slug of the blog.")
        parser.add_argument('--title', default='Blog', help="Title of the blog.")

    def handle_label(self, wxr_file, **options):
        global WP_NS
        self.get_blog_page(options['slug'], options['title'])
        self.auto_excerpt = options.get('auto_excerpt', True)

        self.stdout.write('Starting migration from Wordpress to Puput %s:\n')

        self.tree = ET.parse(wxr_file)
        WP_NS = WP_NS % self.guess_wxr_version(self.tree)

        self.import_authors(self.tree)
        self.categories = self.import_categories(self.tree.findall('channel/{%s}category' % WP_NS))
        self.import_entries(self.tree.findall('channel/item'))

    def guess_wxr_version(self, tree):
        """
        We will try to guess the wxr version used
        to complete the wordpress xml namespace name.
        """
        for v in ('1.2', '1.1', '1.0'):
            try:
                tree.find('channel/{%s}wxr_version' % (WP_NS % v)).text
                return v
            except AttributeError:
                pass
        raise CommandError('Cannot resolve the wordpress namespace')

    def import_authors(self, tree):
        """
        Retrieve all the authors used in posts
        and convert it to new or existing author and
        return the conversion.
        """

        self.stdout.write('- Importing authors\n')

        post_authors = set()
        for item in tree.findall('channel/item'):
            post_type = item.find('{%s}post_type' % WP_NS).text
            if post_type == 'post':
                post_authors.add(item.find(
                    '{http://purl.org/dc/elements/1.1/}creator').text)

        self.stdout.write('> %i authors found.\n' % len(post_authors))

        self.authors = {}
        for post_author in post_authors:
            self.authors[post_author] = self.migrate_author(post_author.replace(' ', '-'))

    def migrate_author(self, author_name):
        """
        Handle actions for migrating the authors.
        """

        action_text = "The author '%s' needs to be migrated to an user:\n" \
                      "1. Use an existing user ?\n" \
                      "2. Create a new user ?\n" \
                      "Please select a choice: " % author_name
        while True:
            selection = str(input(action_text))
            if selection and selection in '12':
                break
        if selection == '1':
            users = User.objects.all()
            if users.count() == 1:
                username = users[0].get_username()
                preselected_user = username
                usernames = [username]
                usernames_display = ['[%s]' % username]
            else:
                usernames = []
                usernames_display = []
                preselected_user = None
                for user in users:
                    username = user.get_username()
                    if username == author_name:
                        usernames_display.append('[%s]' % username)
                        preselected_user = username
                    else:
                        usernames_display.append(username)
                    usernames.append(username)
            while True:
                user_text = "1. Select your user, by typing " \
                            "one of theses usernames:\n" \
                            "%s or 'back'\n" \
                            "Please select a choice: " % \
                            ', '.join(usernames_display)
                user_selected = raw_input(user_text)
                if user_selected in usernames:
                    break
                if user_selected == '' and preselected_user:
                    user_selected = preselected_user
                    break
                if user_selected.strip() == 'back':
                    return self.migrate_author(author_name)
            return users.get(**{users[0].USERNAME_FIELD: user_selected})
        else:
            create_text = "2. Please type the email of " \
                          "the '%s' user or 'back': " % author_name
            author_mail = raw_input(create_text)
            if author_mail.strip() == 'back':
                return self.migrate_author(author_name)
            try:
                return User.objects.create_user(author_name, author_mail)
            except IntegrityError:
                return User.objects.get(**{User.USERNAME_FIELD: author_name})

    def get_blog_page(self, slug, title):
        # Create blog page
        try:
            self.blogpage = BlogPage.objects.get(slug=slug)
        except BlogPage.DoesNotExist:
            # Get root page
            rootpage = Page.objects.first()

            # Set site root page as root site page
            site = Site.objects.first()
            site.root_page = rootpage
            site.save()

            # Get blogpage content type
            self.blogpage = BlogPage(
                title=title,
                slug=slug,
            )
            rootpage.add_child(instance=self.blogpage)
            revision = rootpage.save_revision()
            revision.publish()

    def import_categories(self, category_nodes):
        """
        Import all the categories from 'wp:category' nodes,
        because categories in 'item' nodes are not necessarily
        all the categories and returning it in a dict for
        database optimizations.
        """
        self.stdout.write('- Importing categories\n')

        categories = {}
        for category_node in category_nodes:
            title = category_node.find('{%s}cat_name' % WP_NS).text[:255]
            slug = category_node.find(
                '{%s}category_nicename' % WP_NS).text[:255]
            try:
                parent = category_node.find(
                    '{%s}category_parent' % WP_NS).text[:255]
            except TypeError:
                parent = None
            self.stdout.write('> %s... ' % title)
            category, created = PuputCategory.objects.update_or_create(name=title, defaults={
                'slug': slug, 'parent': categories.get(parent)
            })
            categories[title] = category
            self.stdout.write('OK\n')
        return categories

    def get_entry_tags(self, tags, page):
        """
        Return a list of entry's tags,
        by using the nicename for url compatibility.
        """
        for tag in tags:
            domain = tag.attrib.get('domain', 'category')
            if 'tag' in domain and tag.attrib.get('nicename'):
                puput_tag, created = PuputTag.objects.update_or_create(name=tag.text)
                page.entry_tags.add(PuputTagEntryPage(tag=puput_tag))

    def get_entry_categories(self, category_nodes, page):
        """
        Return a list of entry's categories
        based on imported categories.
        """
        for category_node in category_nodes:
            domain = category_node.attrib.get('domain')
            if domain == 'category':
                puput_category = PuputCategory.objects.get(name=category_node.text)
                PuputCategoryEntryPage.objects.get_or_create(category=puput_category, page=page)

    def import_entry(self, title, content, items, item_node):
        """
        Importing an entry but some data are missing like
        related entries, start_publication and end_publication.
        start_publication and creation_date will use the same value,
        wich is always in Wordpress $post->post_date.
        """
        creation_date = datetime.strptime(
            item_node.find('{%s}post_date' % WP_NS).text,
            '%Y-%m-%d %H:%M:%S')
        if settings.USE_TZ:
            creation_date = timezone.make_aware(
                creation_date, pytz.timezone('GMT'))

        excerpt = strip_tags(item_node.find(
            '{%sexcerpt/}encoded' % WP_NS).text or '')
        if not excerpt:
            if self.auto_excerpt:
                excerpt = Truncator(strip_tags(content)).words(50)
            else:
                excerpt = ''

        # Prefer use this function than
        # item_node.find('{%s}post_name' % WP_NS).text
        # Because slug can be not well formated
        slug = slugify(title)[:255] or 'post-%s' % item_node.find(
            '{%s}post_id' % WP_NS).text
        creator = item_node.find('{http://purl.org/dc/elements/1.1/}creator').text

        # Create page
        try:
            page = EntryPage.objects.get(slug=slug)
        except EntryPage.DoesNotExist:
            page = EntryPage(
                title=title,
                body=content,
                excerpt=strip_tags(content),
                slug=slug,
                go_live_at=datetime.strptime(
                    item_node.find('{%s}post_date_gmt' % WP_NS).text,
                    '%Y-%m-%d %H:%M:%S'),
                first_published_at=creation_date,
                date=creation_date,
                owner=self.authors.get(creator),
                seo_title=title,
                search_description=excerpt,
                live=item_node.find(
                    '{%s}status' % WP_NS).text == 'publish')
            self.blogpage.add_child(instance=page)
            revision = self.blogpage.save_revision()
            revision.publish()
        self.get_entry_tags(item_node.findall('category'), page)
        self.get_entry_categories(item_node.findall('category'), page)
        # Import header image
        image_id = self.find_image_id(item_node.findall('{%s}postmeta' % WP_NS))
        if image_id:
            self.import_header_image(page, items, image_id)
        page.save()
        page.save_revision(changed=False)

    def find_image_id(self, metadatas):
        for meta in metadatas:
            if meta.find('{%s}meta_key' % WP_NS).text == '_thumbnail_id':
                return meta.find('{%s}meta_value' % WP_NS).text

    def import_entries(self, items):
        """
        Loops over items and find entry to import,
        an entry need to have 'post_type' set to 'post' and
        have content.
        """
        self.stdout.write('- Importing entries\n')

        for item_node in items:
            title = (item_node.find('title').text or '')[:255]
            post_type = item_node.find('{%s}post_type' % WP_NS).text
            content = item_node.find('{http://purl.org/rss/1.0/modules/content/}encoded').text

            if post_type == 'post' and content and title:
                self.stdout.write('> %s... ' % title)
                content = self.process_content_image(content)
                self.import_entry(title, content, items, item_node)

    def _import_image(self, image_url):
        img = NamedTemporaryFile(delete=True)
        img.write(urlopen(image_url).read())
        img.flush()
        return img

    def import_header_image(self, entry, items, image_id):
        self.stdout.write('\tImport header images....')
        for item in items:
            post_type = item.find('{%s}post_type' % WP_NS).text
            if post_type == 'attachment' and item.find('{%s}post_id' % WP_NS).text == image_id:
                title = item.find('title').text
                self.stdout.write(' > %s... ' % title)
                image_url = item.find('{%s}attachment_url' % WP_NS).text.encode('utf-8')
                img = self._import_image(image_url)
                new_image = WagtailImage(file=File(file=img, name=title), title=title)
                new_image.save()
                self.stdout.write('\t\t{}'.format(new_image.file.url))
                entry.header_image = new_image
                entry.save()

    def _image_to_embed(self, image):
        return '<embed alt="{}" embedtype="image" format="fullwidth" id="{}"/>'.format(image.title, image.id)

    def process_content_image(self, content):
        self.stdout.write('\tGenerate and replace entry content images....')
        if content:
            root = lxml.html.fromstring(content)
            for img_node in root.iter('img'):
                parent_node = img_node.getparent()
                if 'wp-content' in img_node.attrib['src'] or 'files' in img_node.attrib['src']:
                    img = self._import_image(img_node.attrib['src'])
                    title = img_node.attrib.get('title') or img_node.attrib.get('alt')
                    new_image = WagtailImage(file=File(file=img, name=title), title=title)
                    new_image.save()
                    if parent_node.tag == 'a':
                        parent_node.addnext(ET.XML(self._image_to_embed(new_image)))
                        parent_node.drop_tree()
                    else:
                        parent_node.append(ET.XML(self._image_to_embed(new_image)))
                        img_node.drop_tag()
            content = ET.tostring(root)
        return content

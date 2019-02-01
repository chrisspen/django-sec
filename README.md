Django-SEC
==========

[![](https://img.shields.io/pypi/v/django-sec.svg)](https://pypi.python.org/pypi/django-sec) [![Build Status](https://img.shields.io/travis/chrisspen/django-sec.svg?branch=master)](https://travis-ci.org/chrisspen/django-sec) [![](https://pyup.io/repos/github/chrisspen/django-sec/shield.svg)](https://pyup.io/repos/github/chrisspen/django-sec)

This is a Django app that downloads all SEC filings from the [EDGAR database](https://www.sec.gov/edgar/searchedgar/accessing-edgar-data.htm)
into your local database. It provides an admin interface to allow you to
control which indexes and attributes are loaded as well as inspect downloaded
data.

This is a fork of Luke Rosiak's [PySEC](https://github.com/lukerosiak/pysec),
modified to act as a pluggable Django app with fleshed out admin interface and
more efficient data import commands.

Installation
------------

Install the package using pip via:

    pip install django-sec

then add `django_sec` to your `INSTALLED_APPS` and run:

    python manage.py migrate django_sec

Usage
-----

The data import process is divided into two basic commands.

First, import filing indexes for a target year by running:

    python manage.py sec_import_index --start-year=<year1> --end-year=<year2>
    
This will essentially load the "card catalog" of all companies that filed
documents between those years.

If you're running this on the devserver, you can monitor import progress at:

    http://localhost:8000/admin/django_sec/indexfile/
    
and see the loaded indexes and companies at:

    http://localhost:8000/admin/django_sec/index/
    http://localhost:8000/admin/django_sec/company/

Because the list of companies and filings is enormous, by default, all
companies are configured to not download any actual filings
unless explicitly marked to do so.

To mark companies for download, to go the
company change list page, select one or more companies and run the action
"Enable attribute loading..." Then run:

    python manage.py sec_import_attrs --start-year=<year1> --end-year=<year2>  --form=10-Q,10-K
    
This will download all 10-K and 10-Q filings, extract the attributes and populate
them into the AttributeValue table accessible at:

    http://localhost:8000/admin/django_sec/attributevalue/

Currently, this has only been tested to download and extract attributes from
10-K and 10-Q filings.

The commands support additional parameters and filters, such as to load data
for specific companies or quarters. Run `python manage.py help sec_import_index`
to see all options.

Development
-----------

Tests require the Python development headers to be installed, which you can install on Ubuntu with:

    sudo apt-get install python-dev python3-dev python3.4-dev

To run unittests across multiple Python versions, install:

    sudo apt-get install python3.4-minimal python3.4-dev python3.5-minimal python3.5-dev

To run all [tests](http://tox.readthedocs.org/en/latest/):

    export TESTNAME=; tox

To run tests for a specific environment (e.g. Python 2.7 with Django 1.4):
    
    export TESTNAME=; tox -e py27-django15

To run a specific test:
    
    export TESTNAME=.testname; tox -e py27-django15
    

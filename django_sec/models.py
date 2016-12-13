from __future__ import print_function

import os
import sys
import re
import zipfile

from django.db import models
from django.db.models import Min, Max
from django.conf import settings
from django.utils.translation import ugettext, ugettext_lazy as _

try:
    from admin_steroids.utils import StringWithTitle

    APP_LABEL = StringWithTitle('django_sec', 'SEC')
except ImportError:
    APP_LABEL = 'django_sec'

from django_sec import xbrl

from . import constants as c
from .settings import DATA_DIR


def clean_unit_name(s):
    s = re.sub(r'[^a-z0-9]+', '', str(s).strip().lower())
    return s


class Namespace(models.Model):
    """
    Represents an XBRL namespace used to segment attribute names.
    """

    name = models.CharField(
        # Causes MySQL error:
        # Specified key was too long; max key length is 767 bytes
        # Note, PostreSQL has much higher limits.
        # max_length=255,
        max_length=100,
        blank=False,
        null=False,
        # db_index=True,
        unique=True)

    class Meta:
        app_label = APP_LABEL

    def __unicode__(self):
        return self.name


class UnitManager(models.Manager):
    def get_by_natural_key(self, name, *true_unit_nk):
        true_unit = None
        if true_unit_nk:
            true_unit = Unit.objects.get_by_natural_key(*true_unit_nk)

        u, _ = Unit.objects.get_or_create(name=name)
        u.true_unit = true_unit
        u.save()

        return u


class Unit(models.Model):
    """
    Represents a numeric unit.
    """

    objects = UnitManager()

    name = models.CharField(
        max_length=50,
        blank=False,
        null=False,
        db_index=True,
        unique=True)

    true_unit = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        help_text=_('''Points the the unit record this record duplicates.
            Points to itself if this is the master unit.'''))

    master = models.BooleanField(
        default=True,
        editable=False,
        help_text=_('If true, indicates this unit is the master referred to by duplicates.'))

    class Meta:
        app_label = APP_LABEL
        ordering = (
            'name',
        )

    def __unicode__(self):
        return self.name

    def natural_key(self):
        parts = (self.name,)
        if self.true_unit != self:
            parts += self.true_unit.natural_key()
        return parts

    def save(self, *args, **kwargs):
        assert self.name.strip()
        if self.id:
            self.true_unit = self.true_unit or self
        self.master = self == self.true_unit
        if self.id:
            assert self.true_unit.master
        super(Unit, self).save(*args, **kwargs)

    @classmethod
    def do_update(cls, *args, **kwargs):
        q = cls.objects.filter(true_unit__isnull=True)
        for r in q.iterator():
            r.save()


class Attribute(models.Model):
    """
    Represents a financial attribute tag.
    """

    namespace = models.ForeignKey('Namespace')

    name = models.CharField(
        max_length=200,
        blank=False,
        null=False,
        db_index=True)

    load = models.BooleanField(
        default=False,
        db_index=True,
        help_text=_('If checked, all values will be loaded for this attribute.'))

    total_values = models.PositiveIntegerField(
        blank=True,
        null=True,
        editable=True)

    total_values_fresh = models.BooleanField(
        default=False,
        verbose_name='fresh')

    class Meta:
        app_label = APP_LABEL
        unique_together = (
            ('namespace', 'name'),
        )
        index_together = (
            ('namespace', 'name'),
        )

    def __unicode__(self):
        return '{%s}%s' % (self.namespace, self.name)

    @classmethod
    def do_update(cls, *args, **kwargs):
        q = cls.objects.filter(total_values_fresh=False).only('id', 'name')
        total = q.count()
        i = 0
        for r in q.iterator():
            i += 1
            #            if not i % 100:
            #                print('\rRefreshing attribute %i of %i.' % (i, total),
            #                sys.stdout.flush()
            total_values = AttributeValue.objects.filter(attribute__name=r.name).count()
            cls.objects.filter(id=r.id).update(
                # total_values=r.values.all().count(),
                total_values=total_values,
                total_values_fresh=True)


# print('\rRefreshing attribute %i of %i.' % (total, total),

class AttributeValue(models.Model):
    company = models.ForeignKey('Company', related_name='attributes')

    attribute = models.ForeignKey('Attribute', related_name='values')

    # Inspecting several XBRL samples, no digits above 12 characters
    # or decimals above 5 were found, so I've started there and added
    # a little more to handle future increases.
    value = models.DecimalField(
        max_digits=c.MAX_DIGITS,
        decimal_places=c.MAX_DECIMALS,
        blank=False,
        null=False)

    unit = models.ForeignKey('Unit')

    start_date = models.DateField(
        blank=False,
        null=False,
        db_index=True,
        help_text=_('''If attribute implies a duration, this is the date
            the duration begins. If the attribute implies an instance, this
            is the exact date it applies to.'''))

    end_date = models.DateField(
        blank=True,
        null=True,
        help_text=_('''If this attribute implies a duration, this is the date
            the duration ends.'''))

    filing_date = models.DateField(
        blank=False,
        null=False,
        help_text=_('The date this information became publically available.'))

    class Meta:
        app_label = APP_LABEL
        ordering = ('-attribute__total_values', '-start_date', 'attribute__name')
        unique_together = (
            ('company', 'attribute', 'start_date', 'end_date'),
        )
        index_together = (
            ('company', 'attribute', 'start_date'),
        )

    def __unicode__(self):
        return '%s %s=%s %s on %s' % (
            self.company,
            self.attribute.name,
            self.value,
            self.unit,
            self.start_date,
        )


class IndexFile(models.Model):
    year = models.IntegerField(
        blank=False,
        null=False,
        db_index=True)

    quarter = models.IntegerField(
        blank=False,
        null=False,
        db_index=True)

    filename = models.CharField(max_length=100, blank=False, null=False)

    total_rows = models.PositiveIntegerField(blank=True, null=True)

    processed_rows = models.PositiveIntegerField(blank=True, null=True)

    downloaded = models.DateTimeField(blank=True, null=True)

    processed = models.DateTimeField(blank=True, null=True)

    class Meta:
        app_label = APP_LABEL
        ordering = ('year', 'quarter')
        unique_together = (
            ('year', 'quarter'),
        )


class Company(models.Model):
    cik = models.IntegerField(
        db_index=True,
        primary_key=True,
        help_text=_('Central index key that uniquely identifies a filing entity.'))

    name = models.CharField(
        max_length=100,
        db_index=True,
        blank=False,
        null=False,
        help_text=_('The name of the company.'))

    load = models.BooleanField(
        default=False,
        db_index=True,
        help_text=_('If checked, all values for load-enabled attributes will be loaded for this company.'))

    min_date = models.DateField(
        blank=True,
        null=True,
        editable=False,
        db_index=True,
        help_text=_('''The oldest date of associated SEC Edgar filings
            for this company.'''))

    max_date = models.DateField(
        blank=True,
        null=True,
        editable=False,
        db_index=True,
        help_text=_('''The most recent date of associated SEC Edgar filings
            for this company.'''))

    class Meta:
        app_label = APP_LABEL
        verbose_name_plural = _('companies')

    def __unicode__(self):
        return self.name

    def save(self, *args, **kwargs):
        if self.cik:
            try:
                old = type(self).objects.get(cik=self.cik)

                aggs = self.attributes.all() \
                    .aggregate(Min('start_date'), Max('start_date'))
                self.min_date = aggs['start_date__min']
                self.max_date = aggs['start_date__max']

                if not old.load and self.load:
                    # If we just flag this company for loading then
                    # flag this company's indexes for loading.
                    Index.objects.filter(
                        company=self, attributes_loaded=True
                    ).update(attributes_loaded=False)
            except type(self).DoesNotExist:
                pass
        super(Company, self).save(*args, **kwargs)


class Index(models.Model):
    company = models.ForeignKey(
        'Company',
        related_name='filings')

    form = models.CharField(
        max_length=10,
        blank=True,
        db_index=True,
        verbose_name=_('form type'),
        help_text=_('The type of form the document is classified as.'))

    date = models.DateField(
        blank=False,
        null=False,
        db_index=True,
        verbose_name=_('date filed'),
        help_text=_('The date the item was filed with the SEC.'))

    filename = models.CharField(
        max_length=100,
        blank=False,
        null=False,
        db_index=True,
        help_text=_('The name of the associated financial filing.'))

    year = models.IntegerField(
        blank=False,
        null=False,
        db_index=True)

    quarter = models.IntegerField(
        blank=False,
        null=False,
        db_index=True)

    _ticker = models.CharField(
        max_length=50,
        db_index=True,
        db_column='ticker',
        verbose_name=_('ticker'),
        blank=True,
        null=True,
        help_text=_('''Caches the trading symbol if one is detected in the
            filing during attribute load.'''))

    attributes_loaded = models.BooleanField(default=False, db_index=True)

    valid = models.BooleanField(
        default=True,
        db_index=True,
        help_text=_('If false, errors were encountered trying to parse the associated files.'))

    error = models.TextField(blank=True, null=True)

    class Meta:
        app_label = APP_LABEL
        verbose_name_plural = _('indexes')
        unique_together = (
            # Note, filenames are not necessarily unique.
            # Filenames may be listed more than once under a different
            # form type.
            ('company', 'form', 'date', 'filename', 'year', 'quarter'),
        )
        index_together = (
            ('year', 'quarter'),
            ('company', 'date', 'filename'),
        )
        ordering = ('-date', 'filename')

    def xbrl_link(self):
        if self.form.startswith('10-K') or self.form.startswith('10-Q'):
            id = self.filename.split('/')[-1][:-4]
            return 'http://www.sec.gov/Archives/edgar/data/%s/%s/%s-xbrl.zip' % (
            self.company.cik, id.replace('-', ''), id)
        return None

    def html_link(self):
        return 'http://www.sec.gov/Archives/%s' % self.filename

    def index_link(self):
        id = self.filename.split('/')[-1][:-4]
        return 'http://www.sec.gov/Archives/edgar/data/%s/%s/%s-index.htm' % (self.company.cik, id.replace('-', ''), id)

    def txt(self):
        return self.filename.split('/')[-1]

    def localfile(self):
        filename = '%s/%s/%s/%s' % (DATA_DIR, self.company.cik, self.txt()[:-4], self.txt())
        if os.path.exists(filename):
            return filename
        return None

    def localpath(self):
        return '%s/%s/%s/' % (DATA_DIR, self.company.cik, self.txt()[:-4])

    def localcik(self):
        return '%s/%s/' % (DATA_DIR, self.company.cik)

    def html(self):
        filename = self.localfile()
        if not filename:
            return None
        f = open(filename, 'r').read()
        f_lower = f.lower()
        try:
            return f[f_lower.find('<html>'):f_lower.find('</html>') + 4]
        except:
            print('html tag not found')
            return f

    def download(self, verbose=False):

        d = self.localcik()
        if not os.path.isdir(d):
            os.makedirs(d)

        d = self.localpath()
        if not os.path.isdir(d):
            os.makedirs(d)

        os.chdir(self.localpath())

        html_link = self.html_link()
        xbrl_link = self.xbrl_link()
        if verbose:
            print('xbrl_link:', xbrl_link)

        # if not os.path.exists(html_link.split('/')[-1]):
        #            os.system('wget %s' % html_link)

        if xbrl_link:
            if not os.path.exists(xbrl_link.split('/')[-1]):
                print(xbrl_link)
                if verbose:
                    os.system('wget %s' % xbrl_link)
                else:
                    os.system('wget --quiet %s' % xbrl_link)
                    # Don't to this. It wastes disk space. Just read the ZIP directly.
                    # os.system('unzip *.zip')

    def xbrl_localpath(self):
        try:
            os.chdir(self.localpath())
        except:
            self.download()
        files = os.listdir('.')
        #        print('files:',files
        archives = [elem for elem in files if elem.endswith('.zip')]
        if not archives:
            return None, None
        zf = zipfile.ZipFile(archives[0])
        # xml = sorted([elem for elem in files if elem.endswith('.xml')],key=len)
        xml = sorted([elem for elem in zf.namelist() if elem.endswith('.xml')], key=len)
        #        print('xml:',xml
        #        sys.exit()
        if not len(xml):
            return None, None
        # return self.localpath() + xml[0], zf.open
        return xml[0], zf.open

    def xbrl(self):
        filepath, open_method = self.xbrl_localpath()
        #        print('filepath:',filepath
        if not filepath:
            print('no xbrl found. this option is for 10-ks.')
            return
        x = xbrl.XBRL(filepath, opener=open_method)
        x.fields['FiscalPeriod'] = x.fields['DocumentFiscalPeriodFocus']
        x.fields['FiscalYear'] = x.fields['DocumentFiscalYearFocus']
        x.fields['DocumentPeriodEndDate'] = x.fields['BalanceSheetDate']
        x.fields['PeriodStartDate'] = x.fields['IncomeStatementPeriodYTD']
        x.fields['SECFilingPage'] = self.index_link()
        x.fields['LinkToXBRLInstance'] = self.xbrl_link()

        return x

    def ticker(self):
        """
        Retrieves the company's stock ticker from an XML filing.
        Note, this is not guaranteed to exist.
        """
        if self._ticker:
            return self._ticker
        filepath, _ = self.xbrl_localpath()
        if filepath:
            ticker = filepath.split('/')[-1].split('-')[0].strip().upper()
            if ticker:
                self._ticker = ticker
            else:
                self._ticker = None
        return self._ticker

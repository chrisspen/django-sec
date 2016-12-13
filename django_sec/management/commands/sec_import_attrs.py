from __future__ import print_function

import urllib
import os
import re
import sys
from zipfile import ZipFile
import time
from datetime import date, datetime, timedelta
from optparse import make_option
import traceback
import random
from multiprocessing import Process, Lock, Queue
import collections

from six import StringIO

from django.core.management.base import BaseCommand
from django.db import transaction, connection, IntegrityError, DatabaseError
from django.conf import settings
from django.utils import timezone

from django_sec import models
from django_sec.models import DATA_DIR, c

try:
    from psycopg2.extensions import TransactionRollbackError
except ImportError:
    TransactionRollbackError = Exception

try:
    from chroniker.models import Job
except ImportError:
    Job = None


def is_power_of_two(x):
    return (x & (x - 1)) == 0


def parse_stripe(stripe):
    stripe_num = None
    stripe_mod = None
    if stripe:
        assert isinstance(stripe, str) and len(stripe) == 2
        stripe_num, stripe_mod = stripe
        stripe_num = int(stripe_num)
        stripe_mod = int(stripe_mod)
        assert stripe_num < stripe_mod
    return stripe_num, stripe_mod


class Command(BaseCommand):
    help = "Shows data from filings."
    args = ''

    def add_arguments(self, parser):
        parser.add_argument('--cik', default=None, dest='cik', action='store')
        parser.add_argument('--forms', default='10-K,10-Q', dest='forms', action='store')
        parser.add_argument('--start-year', default=None, dest='start_year', action='store')
        parser.add_argument('--end-year', default=None, dest='end_year', action='store')
        parser.add_argument('--quarter', default=None, dest='quarter', action='store')
        parser.add_argument('--force', default=False, dest='force', action='store_true')
        parser.add_argument('--verbose', default=False, dest='verbose', action='store_true')
        parser.add_argument('--multi', default=0, dest='multi', action='store',
                            help='The number of processes to use. Must be a multiple of 2.')
        parser.add_argument('--show-pending', action='store_true', dest='show_pending', default=False,
                            help='If given, will only report the number of pending records to process then exit.')

    def handle(self, **options):
        self.force = options['force']
        self.verbose = options['verbose']

        self.stripe_counts = {}  # {stripe:{current,total}
        self.last_progress_refresh = None
        self.start_times = {}  # {key:start_time}

        self.cik = (options['cik'] or '').strip()
        if self.cik:
            self.cik = int(self.cik)
        else:
            self.cik = None

        self.forms = (options['forms'] or '').strip().split(',')

        start_year = options['start_year']
        if start_year:
            start_year = int(start_year)
        else:
            # start_year = date.today().year - 1
            start_year = 1900
        self.start_year = start_year

        end_year = options['end_year']
        if end_year:
            end_year = int(end_year)
        else:
            end_year = date.today().year
        self.end_year = end_year

        self.status = None
        self.progress = collections.OrderedDict()
        multi = int(options['multi'])
        kwargs = options.copy()
        if multi:
            assert multi > 1 and is_power_of_two(multi), \
                "Process count must be greater than 1 and a multiple of 2."
            processes = []
            self.status = Queue()
            for i, _ in enumerate(range(multi)):
                print('Starting process %i' % i)
                stripe = kwargs['stripe'] = '%i%i' % (i, multi)
                kwargs['status'] = self.status

                connection.close()
                p = Process(target=self.run_process, kwargs=kwargs)
                p.daemon = True
                processes.append(p)
                p.start()
            self.progress[stripe] = (0, 0, 0, 0, None, '')
            # return
            while any(i.is_alive() for i in processes):
                time.sleep(0.1)
                while not self.status.empty():
                    stripe, current, total, sub_current, sub_total, eta, message = self.status.get()
                    self.progress[stripe] = (current, total, sub_current, sub_total, eta, message)
                    if stripe not in self.start_times:
                        self.start_times[stripe] = time.time()
                    self.print_progress()
            print('All processes complete.')
        else:
            self.start_times[None] = time.time()
            self.run_process(**kwargs)

    def print_progress(self, clear=True, newline=True):
        if self.last_progress_refresh and (datetime.now() - self.last_progress_refresh).seconds < 0.5:
            return
        bar_length = 10
        if clear:
            sys.stdout.write('\033[2J\033[H')  # clear screen
            sys.stdout.write('Importing attributes\n')
        for stripe, (current, total, sub_current, sub_total, eta, message) in sorted(self.progress.items()):
            sub_status = ''
            if total:
                if not eta:
                    start_time = self.start_times[stripe]
                    current_seconds = time.time() - start_time
                    total_seconds = float(total) / current * current_seconds
                    remaining_seconds = int(total_seconds - current_seconds)
                    eta = timezone.now() + timedelta(seconds=remaining_seconds)

                self.stripe_counts[stripe] = (current, total)
                percent = current / float(total)
                bar = ('=' * int(percent * bar_length)).ljust(bar_length)
                percent = int(percent * 100)
            else:
                eta = eta or '?'
                percent = 0
                bar = ('=' * int(percent * bar_length)).ljust(bar_length)
                percent = '?'
                total = '?'
            if sub_current and sub_total:
                sub_status = '(subtask %s of %s) ' % (sub_current, sub_total)
            sys.stdout.write(
                (('' if newline else '\r') + "%s [%s] %s of %s %s%s%% eta=%s: %s" + ('\n' if newline else '')) \
                % (stripe, bar, current, total, sub_status, percent, eta, message))
        sys.stdout.flush()
        self.last_progress_refresh = datetime.now()

        # Update job.
        overall_current_count = 0
        overall_total_count = 0
        for stripe, (current, total) in self.stripe_counts.items():
            overall_current_count += current
            overall_total_count += total
        # print('overall_current_count:',overall_current_count
        # print('overall_total_count:',overall_total_count
        if overall_total_count and Job:
            Job.update_progress(
                total_parts_complete=overall_current_count,
                total_parts=overall_total_count,
            )
            #             if not self.dryrun:
            #                 transaction.commit()

    def run_process(self, status=None, **kwargs):
        tmp_debug = settings.DEBUG
        settings.DEBUG = False
        # transaction.enter_transaction_management()
        # transaction.managed(True)
        try:
            print('Running process:', kwargs)
            self.import_attributes(status=status, **kwargs)
            print('Done process:', kwargs)
        finally:
            settings.DEBUG = tmp_debug
            # if self.dryrun:
            #    print('This is a dryrun, so no changes were committed.'
            #    transaction.rollback()
            # else:
            #    transaction.commit()
            # transaction.leave_transaction_management()
            connection.close()

    def import_attributes(self, status=None, **kwargs):
        stripe = kwargs.get('stripe')
        reraise = kwargs.get('reraise')

        current_count = 0
        total_count = 0
        fatal_errors = False
        fatal_error = None
        estimated_completion_datetime = None
        sub_current = 0
        sub_total = 0

        def print_status(message, count=None, total=None):
            # print('message:',message
            current_count = count or 0
            total_count = total or 0
            if status:
                status.put([
                    stripe,
                    current_count + 1,
                    total_count,
                    sub_current,
                    sub_total,
                    estimated_completion_datetime,
                    message,
                ])
            else:
                # print('total_count:',total_count
                self.progress[stripe] = (
                    current_count,
                    total_count,
                    sub_current,
                    sub_total,
                    estimated_completion_datetime,
                    message,
                )
                self.print_progress(clear=False, newline=True)

        stripe_num, stripe_mod = parse_stripe(stripe)
        if stripe:
            print_status('Striping with number %i and modulus %i.' \
                         % (stripe_num, stripe_mod))

        try:
            # Get a file from the index.
            # It may or may not be present on our hard disk.
            # If it's not, it will be downloaded
            # the first time we try to access it, or you can call
            # .download() explicitly.
            q = models.Index.objects.filter(
                year__gte=self.start_year,
                year__lte=self.end_year)
            if not self.force:
                q = q.filter(
                    attributes_loaded__exact=0,  # False,
                    valid__exact=1,  # True,
                )
            if self.forms:
                q = q.filter(form__in=self.forms)

            q2 = q
            if self.cik:
                q = q.filter(company__cik=self.cik, company__load=True)
                q2 = q2.filter(company__cik=self.cik)
                if not q.count() and q2.count():
                    print >> sys.stderr, 'Warning: the company you specified with cik %s is not marked for loading.' % (
                    self.cik,)

            if stripe is not None:
                # q = q.extra(where=['(("django_sec_index"."id" %%%% %i) = %i)' % (stripe_mod, stripe_num)])
                q = q.extra(where=['((django_sec_index.id %%%% %i) = %i)' % (stripe_mod, stripe_num)])

            # print_status('Finding total record count...')
            # print('query:', q.query
            total_count = total = q.count()
            print('total_count:', total_count)

            if kwargs['show_pending']:
                print('=' * 80)
                print('%i total pending records' % total_count)
                return

            print_status('%i total rows.' % (total,))
            i = 0
            commit_freq = 100
            print_status('%i indexes found for forms %s.' % (total, ', '.join(self.forms)), count=0, total=total)
            for ifile in q.iterator():
                i += 1
                current_count = i

                # print('Processing index %s for (%i of %i)' % (ifile.filename, i, total)
                msg = 'Processing index %s.' % (ifile.filename,)
                print_status(msg, count=i, total=total)

                if not i % commit_freq:
                    sys.stdout.flush()
                # if not self.dryrun:
                #                         transaction.commit()

                ifile.download(verbose=self.verbose)
                # print('xbrl link:',ifile.xbrl_link()

                # Initialize XBRL parser and populate an attribute called fields with
                # a dict of 50 common terms.
                x = None
                error = None
                try:
                    x = ifile.xbrl()
                except Exception as e:
                    ferr = StringIO()
                    traceback.print_exc(file=ferr)
                    error = ferr.getvalue()

                if x is None:
                    if error is None:
                        error = 'No XBRL found.'
                    models.Index.objects.filter(id=ifile.id) \
                        .update(valid=False, error=error)
                    continue

                maxretries = 10
                retry = 0
                # for retry in xrange(maxretries):
                while 1:
                    try:

                        # x.loadYear(2)
                        #            print'Year:', x.fields['FiscalYear']
                        company = ifile.company
                        max_text_len = 0
                        unique_attrs = set()
                        bulk_objects = []
                        prior_keys = set()
                        j = sub_total = 0
                        # print
                        for node, sub_total in x.iter_namespace():
                            j += 1
                            sub_current = j
                            if not j % commit_freq:
                                # print('\rImporting attribute %i of %i.' % (j, sub_total),
                                print_status(msg, count=i, total=total)
                                # sys.stdout.flush()
                            #                                 if not self.dryrun:
                            #                                     transaction.commit()

                            matches = re.findall('^\{([^\}]+)\}(.*)$', node.tag)
                            if matches:
                                ns, attr_name = matches[0]
                            else:
                                ns = None
                                attr_name = node
                            decimals = node.attrib.get('decimals', None)
                            if decimals is None:
                                continue
                            if decimals.upper() == 'INF':
                                decimals = 6
                            decimals = int(decimals)
                            max_text_len = max(max_text_len, len((node.text or '').strip()))
                            context_id = node.attrib['contextRef']
                            #            if context_id != 'D2009Q4YTD':
                            #                continue
                            start_date = x.get_context_start_date(context_id)
                            if not start_date:
                                continue
                            end_date = x.get_context_end_date(context_id)
                            if not end_date:
                                continue
                            namespace, _ = models.Namespace.objects.get_or_create(name=ns.strip())
                            attribute, _ = models.Attribute.objects.get_or_create(
                                namespace=namespace,
                                name=attr_name,
                                defaults=dict(load=True),
                            )
                            if not attribute.load:
                                continue
                            unit, _ = models.Unit.objects.get_or_create(name=node.attrib['unitRef'].strip())
                            unit.save()
                            value = (node.text or '').strip()
                            if not value:
                                continue
                            assert len(value.split('.')[0]) <= c.MAX_QUANTIZE, \
                                'Value too large, must be less than %i digits: %i %s' \
                                % (c.MAX_QUANTIZE, len(value), repr(value))

                            # print(attribute
                            models.Attribute.objects.filter(id=attribute.id).update(total_values_fresh=False)
                            # print(context_id,attribute.name,node.attrib['decimals'],unit,start_date,end_date,ifile.date

                            if models.AttributeValue.objects.filter(company=company, attribute=attribute,
                                                                    start_date=start_date).exists():
                                continue

                            # Some attributes are listed multiple times in differently
                            # named contexts even though the value and date ranges are
                            # identical.
                            key = (company, attribute, start_date)
                            if key in prior_keys:
                                continue
                            prior_keys.add(key)

                            bulk_objects.append(models.AttributeValue(
                                company=company,
                                attribute=attribute,
                                start_date=start_date,
                                end_date=end_date,
                                value=value,
                                unit=unit,
                                filing_date=ifile.date,
                            ))

                            if not len(bulk_objects) % commit_freq:
                                models.AttributeValue.objects.bulk_create(bulk_objects)
                                bulk_objects = []
                                prior_keys.clear()

                                #                         if not self.dryrun:
                                #                             transaction.commit()
                                #            print('\rImporting attribute %i of %i.' % (sub_total, sub_total),
                                #            print
                        print_status('Importing attributes.', count=i, total=total)

                        if bulk_objects:
                            models.AttributeValue.objects.bulk_create(bulk_objects)
                            bulk_objects = []

                        ticker = ifile.ticker()
                        models.Index.objects.filter(id=ifile.id).update(attributes_loaded=True, _ticker=ticker)
                        models.Attribute.do_update()
                        models.Unit.do_update()
                        break

                    except DatabaseError as e:
                        if retry + 1 == maxretries:
                            raise
                        print(e, 'retry', retry)
                        connection.close()
                        time.sleep(random.random() * 5)

                    except TransactionRollbackError as e:
                        if TransactionRollbackError.__name__ != 'TransactionRollbackError':
                            raise
                        if retry + 1 == maxretries:
                            raise
                        print(e, 'retry', retry)
                        connection.close()
                        time.sleep(random.random() * 5)

        except Exception as e:
            print('Error: %s' % e)
            ferr = StringIO()
            traceback.print_exc(file=ferr)
            error = ferr.getvalue()
            print_status('Fatal error: %s' % (error,))
        finally:
            connection.close()

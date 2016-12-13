from __future__ import print_function

import urllib.request
from contextlib import closing
import shutil
import os
from ftplib import FTP
import sys
from zipfile import ZipFile
from datetime import date, datetime, timedelta
from django.core.management.base import BaseCommand
from django.db import transaction, connection
from django.conf import settings
from django.utils import timezone
from django.utils.encoding import force_text

from django_sec.models import Company, Index, IndexFile, DATA_DIR

def removeNonAscii(s):
    return "".join(i for i in s if ord(i)<128)

class Command(BaseCommand):
    help = "Download new files representing one month of 990s, ignoring months we already have. "\
        "Each quarter contains hundreds of thousands of filings; will take a while to run. "
    def add_arguments(self, parser):
        parser.add_argument('--start_year', default=None, dest='start_year', action='store')
        parser.add_argument('--end_year', default=None, dest='end_year', action='store')
        parser.add_argument('--quarter', default=None, dest='quarter', action='store')
        parser.add_argument('--delete-prior-indexes', action='store_true', default=False, dest='delete_prior_indexes')
        parser.add_argument('--reprocess', action='store_true', default=False, dest='reprocess')
        parser.add_argument('--auto-reprocess-last-n-days',
                            help='The number of days to automatically redownload and reprocess index files.',
                            default=90, dest='auto_reprocess_last_n_days', action='store')

    #args = ''
    """
    option_list = NoArgsCommand.option_list + (
        make_option('--start-year',
            default=None),
        make_option('--end-year',
            default=None),
        make_option('--quarter',
            default=None),
        make_option('--delete-prior-indexes',
            action='store_true',
            default=False),
        make_option('--reprocess',
            action='store_true',
            default=False),
        make_option('--auto-reprocess-last-n-days',
            default=90,
            help='The number of days to automatically redownload and reprocess index files.'),
    )"""
    
    def handle(self, **options):
        
        start_year = options['start_year']
        if start_year:
            start_year = int(start_year)
        else:
            start_year = date.today().year - 1
            
        end_year = options['end_year']
        if end_year:
            end_year = int(end_year)
        else:
            end_year = date.today().year+1
        
        reprocess = options['reprocess']
        
        target_quarter = options['quarter']
        if target_quarter:
            target_quarter = int(target_quarter)
        
        auto_reprocess_last_n_days = int(options['auto_reprocess_last_n_days'])
        
        tmp_debug = settings.DEBUG
        settings.DEBUG = False
        try:
            for year in range(start_year, end_year):
                for quarter in range(4):
                    if target_quarter and quarter+1 != target_quarter:
                        continue
                    quarter_start = date(year, quarter*3+1, 1)
                    _reprocess = reprocess or (quarter_start > (date.today() - timedelta(days=auto_reprocess_last_n_days)))
                    self.get_filing_list(year, quarter+1, reprocess=_reprocess)
        finally:
            settings.DEBUG = tmp_debug
            connection.close()
                
    def get_filing_list(self, year, quarter, reprocess=False):
        """
        Gets the list of filings and download locations for the given year and quarter.
        """
        url='ftp://ftp.sec.gov/edgar/full-index/%d/QTR%d/company.zip' % (year, quarter)
        path = "edgar/full-index/%d/QTR%d/company.zip" % (year, quarter)
    
        # Download the data and save to a file
        if not os.path.isdir(DATA_DIR):
            os.makedirs(DATA_DIR)
        fn = os.path.join(DATA_DIR, 'company_%d_%d.zip' % (year, quarter))
    
        ifile, _ = IndexFile.objects.get_or_create(
            year=year, quarter=quarter, defaults=dict(filename=fn))
        if ifile.processed and not reprocess:
            return
        ifile.filename = fn
        
        if os.path.exists(fn) and reprocess:
            print('Deleting old file %s.' % fn)
            os.remove(fn)

        if not os.path.exists(fn):
            print('Downloading %s.' % (url,))

            ftp = FTP('ftp.sec.gov', user="anonymous", passwd="anonymous")

            ftp.retrbinary('RETR %s' % path, open(fn, 'wb').write)
            #urllib.request.urlop
            #with closing(urllib.request.urlopen(url)) as ftp:
            #    with open(fn, 'w') as f:
            #        shutil.copyfileobj(ftp, f)
            ifile.downloaded = timezone.now()
        
        if not ifile.downloaded:
            ifile.downloaded = timezone.now()
        ifile.save()
        transaction.commit()
        
        # Extract the compressed file
        print('Opening index file %s.' % (fn,))
        zip = ZipFile(fn)
        zdata = zip.read('company.idx')
        #zdata = removeNonAscii(zdata)
        
        # Parse the fixed-length fields
        bulk_companies = []
        bulk_indexes = []
        bulk_commit_freq = 1000
        status_secs = 3
        lines = zdata.split('\n')
        i = 0
        total = len(lines)
        IndexFile.objects.filter(id=ifile.id).update(total_rows=total)
        last_status = None
        #prior_keys = set(Index.objects.all().values_list('company__cik','date','filename').distinct())#Massive memory consumption
        prior_keys = set()
        #print('Found %i prior index keys.' % len(prior_keys)
        prior_ciks = set(Company.objects.all().values_list('cik', flat=True))
        print('Found %i prior ciks.' % len(prior_ciks))
        index_add_count = 0
        company_add_count = 0
        for r in lines[10:]: # Note, first 10 lines are useless headers.
            i += 1
            if not reprocess and ifile.processed_rows and i < ifile.processed_rows:
                continue
            if not last_status or ((datetime.now() - last_status).seconds >= status_secs):
            #if not last_status or not i % 100:
                sys.stdout.write('\rProcessing record %i of %i (%.02f%%).' % (i, total, float(i)/total*100))
                sys.stdout.flush()
                last_status = datetime.now()
                IndexFile.objects.filter(id=ifile.id).update(processed_rows=i)
            dt = r[86:98].strip()
            if not dt:
                continue
            dt = date(*map(int, dt.split('-')))
            if r.strip() == '':
                continue
            name = r[0:62].strip()
            
            cik = int(r[74:86].strip())
            if cik not in prior_ciks:
                company_add_count += 1
                prior_ciks.add(cik)
                bulk_companies.append(Company(cik=cik, name=force_text(name, errors='replace')))
                
            filename = r[98:].strip()
            key = (cik, dt, filename)#, year, quarter)
            if key in prior_keys:
                continue
            prior_keys.add(key)
            if Index.objects.filter(company__cik=cik, date=dt, filename=filename).exists():
                continue
            index_add_count += 1
            bulk_indexes.append(Index(
                company_id=cik,
                form=r[62:74].strip(), # form type
                date=dt, # date filed
                year=year,
                quarter=quarter,
                filename=filename,
            ))
            if not len(bulk_indexes) % bulk_commit_freq:
                if len(bulk_companies):
                    Company.objects.bulk_create(bulk_companies)
                    bulk_companies = []
                Index.objects.bulk_create(bulk_indexes)
                bulk_indexes = []
                transaction.commit()
                
        if bulk_indexes:
            if len(bulk_companies):
                Company.objects.bulk_create(bulk_companies)
                bulk_companies = []
            Index.objects.bulk_create(bulk_indexes)
        IndexFile.objects.filter(id=ifile.id).update(processed=timezone.now())
        transaction.commit()
        
        print('\rProcessing record %i of %i (%.02f%%).' % (total, total, 100))
        print()
        print('%i new companies found.' % company_add_count)
        print('%i new indexes found.' % index_add_count)
        sys.stdout.flush()
        IndexFile.objects.filter(id=ifile.id).update(processed_rows=total)
        
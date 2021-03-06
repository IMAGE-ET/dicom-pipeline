import subprocess
import sys
import time
import os
import re
import dicom
import datetime
import shutil
import sqlite3
from optparse import OptionParser
from ruffus import *
from utils import dicom_count
from dicom_anon import dicom_anon

import local_settings as local
from local_settings import *

from django.core.management import setup_environ
setup_environ(local)

from hooks import registry
from dicom_models.staging.models import *

GET_ORIGINAL_QUERY = "SELECT original FROM studyinstanceuid WHERE cleaned = ?"

devnull = None
dicom_store_scp = None
overview = None
run_re = re.compile(r'run_at_(\d+)')
run_dir = os.path.sep.join(['data',"run_at_%d" % int(time.time())])
limit = 0
modalities = None

if __name__ == "__main__":
    parser = OptionParser()
    parser.add_option("-r", "--runlast", default=False, dest="runlast", action="store_true",
            help="Re-run last pipeline.")
    parser.add_option("-m", "--max", default = 10, dest="limit", action="store",
            help="Maximum number of studies to run through pipeline")
    parser.add_option("-p", "--practice", default = False, dest="practice", action="store_true",
            help="Don't modify de-identified studies to include patient aliases, modify the application database, or push to production")
    parser.add_option("-v", "--verbosity", default = 5, dest = "verbosity", action = "store",
            help="Specify pipeline versbosity, 1-10. See Ruffus documentation for details.")
    parser.add_option("-a", "--allowed_modalities", default = "mr,ct", dest = "modalities", action = "store",
            help="Comma separated list of allowed modality types. Defaults to 'MR,CT'")
    parser.add_option("-n", "--no_push", default = False, dest = "no_push", action = "store_true",
            help="Do not push studies to production PACS (stops after registering studies with encounter in database).")
    (options, args) = parser.parse_args()

    try:
        limit = int(options.limit)
    except ValueError:
        print "Max argument must be a number"
        sys.exit()
    modalities = options.modalities.lower()

    if options.runlast:
        if os.path.exists("data"):
            last = 0
            for listing in os.listdir("data"):
                if os.path.isdir(os.path.sep.join(["data",listing])):
                    match = run_re.match(listing)
                    if match:
                        runtime = int(match.group(1))
                        if last < runtime:
                            last = runtime
            if last:
                run_dir = os.path.sep.join(["data", "run_at_%d" % last])

# Reads the file created in get reviewed studies and returns a list of the study uid
def get_study_list():
    file_name = os.path.sep.join([run_dir, "studies_to_retrieve.txt"])
    studies_file = open(file_name, "r")
    studies = studies_file.read().splitlines()
    studies_file.close()
    studies = [line.strip() for line in studies]
    return studies

def setup_data_dir():
    global overview 
    if not os.path.exists(run_dir):
        os.makedirs(run_dir)
    print "Working directory will be %s" % run_dir
    overview = open(os.path.sep.join([run_dir, "overview.txt"]), "a")
    now = datetime.datetime.now()
    overview.write("Starting at %s\n" % now.strftime("%Y-%m-%d %H:%M"))
    overview.flush()
    os.fsync(overview.fileno())

@follows(setup_data_dir)
@files(None, os.path.sep.join([run_dir, "studies_to_retrieve.txt"]))
def get_reviewed_studies(input_file, output_file):
    studies = RadiologyStudy.objects.filter(radiologystudyreview__has_phi = False,
        radiologystudyreview__relevant = True,
        radiologystudyreview__has_reconstruction = False,
        exclude = False,
        image_published = False).exclude(radiologystudyreview__exclude = True).exclude(processing_error = True).distinct()[0:limit]
    
    # Go through and for each study, make sure we do not have any conflicting reviews
    stop = False    
    for study in studies:
        for review in study.radiologystudyreview_set.all():
            if review.has_phi or review.exclude == True or review.relevant == False or review.has_reconstruction == True:
                stop = True
                msg = "Study %d has conflicting reviews, please address manually and continue pipeline. If an issue is found, remove the uid from studies_to_retrieve.txt.\n" % study.original_study_uid
                overview.write(msg)
                overview.flush()
                os.fsync(overview.fileno())
                print msg,

    comments = open(os.path.sep.join([run_dir, "comments.txt"]), "w")
    for study in studies:
        comments.write("%s:\n" % study.original_study_uid)
        for review in study.radiologystudyreview_set.all():
            comments.write("\t%s\n" % review.comment)
    comments.close()

    f = open(output_file, "w")
    for study in studies:
        f.write(study.original_study_uid+"\n")
    f.close()
    overview.write("%d valid reviewed studies. Please review comments.txt\n" % len(studies))
    overview.flush()
    os.fsync(overview.fileno())

    if stop:
        overview.close()
        sys.exit()

@follows(get_reviewed_studies, mkdir(os.path.sep.join([run_dir, "from_staging"])))
@files(os.path.sep.join([run_dir, "studies_to_retrieve.txt"]), os.path.sep.join([run_dir, "pull_output.txt"]))
def start_dicom_server(input_file = None, output_file = None):
    global dicom_store_scp
    global devnull
    devnull = open(os.devnull, 'w')
    dicom_store_scp = subprocess.Popen("dcmrcv %s@%s:%d -dest %s" % (LOCAL_AE, LOCAL_HOST, LOCAL_PORT, os.path.sep.join([run_dir, "from_staging"])), 
            stdout=devnull, shell=True)

@files(os.path.sep.join([run_dir, "studies_to_retrieve.txt"]), os.path.sep.join([run_dir, "pull_output.txt"]))
@follows(start_dicom_server)
def request_dicom_files(input_file, output_file = None):
     results = subprocess.check_output("dicom_tools/retrieve.sh %s@%s:%d %s" % (STAGE_AE, 
         STAGE_HOST, STAGE_PORT, input_file), stderr=subprocess.STDOUT, shell=True)
     f = open(os.path.sep.join([run_dir, "pull_output.txt"]), "w")
     f.write(results)
     f.close()
     overview.write("Received %d files containing %d studies\n" % dicom_count(os.path.sep.join([run_dir, "from_staging"])))
     overview.flush()
     os.fsync(overview.fileno())

@follows(request_dicom_files)
def stop_dicom_server():
    if dicom_store_scp: 
        dicom_store_scp.kill()
        devnull.close()

@files(os.path.sep.join([run_dir, "pull_output.txt"]), os.path.sep.join([run_dir, "anonymize_output.txt"]))
@follows(stop_dicom_server)
def anonymize(input_file = None, output_file = None):
    result = dicom_anon.driver(os.path.sep.join([run_dir, "from_staging"]),
           os.path.sep.join([run_dir, "to_production"]),
           quarantine_dir = os.path.sep.join([run_dir, "quarantine"]),
           audit_file="identity.db",
           allowed_modalities=[x.strip() for x in modalities.split(",")],
           org_root = DICOM_ROOT,
           white_list_file = "dicom_limited_vocab.json",
           log_file=os.path.sep.join([run_dir, "anonymize_in_progress.txt"]),
           overlay = True,
           profile = "clean")

    if result:
       shutil.move(os.path.sep.join([run_dir, "anonymize_in_progress.txt"]), 
           os.path.sep.join([run_dir, "anonymize_output.txt"]))
    else:
       overview.write("Error during anonymization, see anonymize_in_progress.text\n")
       overview.close()
       sys.exit()

@files(os.path.sep.join([run_dir, "anonymize_output.txt"]), os.path.sep.join([run_dir, "missing_protocol_studies.txt"]))
@follows(anonymize)
def check_patient_protocol(input_file = None, output_file = None):
    studies = get_study_list()

    protocol_studies = RadiologyStudy.objects.filter(original_study_uid__in=studies,
        radiologystudyreview__has_phi = False,
        radiologystudyreview__relevant = True,
        radiologystudyreview__has_reconstruction = False,
        exclude = False,
        radiologystudyreview__has_protocol_series = True).distinct()

    reviewed_protocol_studies = set([x.original_study_uid for x in protocol_studies])

    quarantine_dir = os.path.sep.join([run_dir, "quarantine"])
    found_protocol_studies = set()
    for root, dirs, files in os.walk(quarantine_dir):
        for filename in files:
            try:
                ds = dicom.read_file(os.path.join(root, filename))
            except IOError:
                sys.stderr.write("Unable to read %s" % os.path.join(root, filename))
                continue
            series_desc = ds[0x8,0x103E].value.strip().lower()
            if series_desc == "patient protocol":
                study_uid = ds[0x20,0xD].value.strip()
                found_protocol_studies.add(study_uid)

    marked_but_not_found = reviewed_protocol_studies - found_protocol_studies

    overview.write("%d studies marked as having a protocol series, %d studies found with protocol series during anonymization.\n" % (len(reviewed_protocol_studies), len(found_protocol_studies)))
    overview.write("%d studies marked as having a protocol series but not found, see 'missing_protocol_studies.txt'.\n" % len(marked_but_not_found))
    overview.flush()
    os.fsync(overview.fileno())

    f = open(os.path.sep.join([run_dir, "reviewed_protocol_studies.txt"]), "w")
    for study in reviewed_protocol_studies:
        f.write(study+"\n")
    f.close()

    f = open(os.path.sep.join([run_dir, "found_protocol_studies.txt"]), "w")
    for study in found_protocol_studies:
        f.write(study+"\n")
    f.close()

    f = open(os.path.sep.join([run_dir, "missing_protocol_studies.txt"]), "w")
    for study in marked_but_not_found:
        f.write(study+"\n")
    f.close()

@files(os.path.sep.join([run_dir, "missing_protocol_studies.txt"]), os.path.sep.join([run_dir, "post_anon_output.txt"]))
@follows(check_patient_protocol)
def post_anon(input_file = None, output_file = None):
    results = registry.get(local.POST_ANON_HOOK)(run_dir, overview, options.practice) 
    if options.practice:
        f = open(os.path.sep.join([run_dir, "post_anon_output_practice.txt"]), "w")
    else:
        f = open(os.path.sep.join([run_dir, "post_anon_output.txt"]), "w")
    f.write(results+"\n")
    f.close()

@files(os.path.sep.join([run_dir, "post_anon_output.txt"]), os.path.sep.join([run_dir, "push_output.txt"]))
@follows(post_anon)
def push_to_production(input_file = None, output_file = None):

    results = subprocess.check_output("dcmsnd %s@%s:%d %s" % (PROD_AE, PROD_HOST, PROD_PORT, os.path.sep.join([run_dir, "to_production"])), 
        shell=True)

    f = open(os.path.sep.join([run_dir, "push_output.txt"]), "w")
    f.write(results)
    f.close()
    now = datetime.datetime.now()
    overview.write("Push completed at %s\n" % now.strftime("%Y-%m-%d %H:%M"))
    overview.flush()
    os.fsync(overview.fileno())


@files(os.path.sep.join([run_dir, "push_output.txt"]), os.path.sep.join([run_dir, "done.txt"]))
@follows(push_to_production)
def set_as_pushed(input_file=None, output_file=None):
    now = datetime.datetime.now()
    production_dir = os.path.sep.join([run_dir, "to_production"])
    studies = set()
    for root, dirs, files in os.walk(production_dir):
        for filename in files:
            if filename.startswith('.'):
                continue
            try:
                ds = dicom.read_file(os.path.join(root,filename))
            except IOError:
                sys.stderr.write("Unable to read %s" % os.path.join(root, filename))
                continue

            study_uid = ds[0x20,0xD].value.strip()
            if not study_uid in studies:
                studies.add(study_uid)

    conn = sqlite3.connect('identity.db')
    c = conn.cursor()

    pushed_studies = set()
    for study in studies:
        result = c.execute(GET_ORIGINAL_QUERY, (study,))
        try:
            # We need to get the original id so we can determine if there were any errors are mark the studies
            original = result.fetchall()[0][0]
            pushed_studies.add(original.strip())
        except:
            sys.stderr.write("Unable to get original study_uid for %s while trying to reconcile pushed studies with errored studies." % study)
            overview.write("Unable to get original study_uid for %s while trying to reconcile pushed studies with errored studies.\n" % study)
            overview.flush()
            os.fsync(overview.fileno())
            continue

        rs = RadiologyStudy.objects.get(study_uid=study)
        rs.image_published = True
        rs.save()

    conn.close()
    overview.write("%d studies marked as pushed\n" % len(studies))
    overview.flush()
    os.fsync(overview.fileno())
    # Mark any studies that were not pushed as processing_error in the database so we
    # don't keep pulling them over and over
    requested_studies = set(get_study_list())

    # Subtract studies we requested from studies we pushed
    failed_studies = requested_studies - pushed_studies
    for study_uid in failed_studies:
        try:
            rs = RadiologyStudy.objects.get(original_study_uid = study_uid) 
        except ObjectDoesNotExist:
            sys.stderr.write("Tried to mark study %s as error, but unable to find study object" % study_uid)
            continue
        # If it was marked as pushed, it was successfully pushed. 
        # If it is already marked as error, we have a more specific error message for it
        # provided by another part of the pipeline
        if not rs.processing_error and not rs.image_published:
            rs.processing_error = True
            rs.processing_error_date = now
            rs.processing_error_msg = "Error during anonymization. Most likely this study was never found in the source PACS"
            rs.save()
    if len(failed_studies) > 0:
         overview.write("%d studies were not pushed:\n %s\n" % (len(failed_studies), failed_studies))

    f = open(os.path.sep.join([run_dir, "done.txt"]), "w")
    now = datetime.datetime.now()
    f.write("Pipeline completed at %s\n" % now.strftime("%Y-%m-%d %H:%M"))
    f.close()

def main():
    if options.no_push or options.practice:
        pipeline_run([post_anon], verbose = options.verbosity)
    else:
        pipeline_run([set_as_pushed], verbose = options.verbosity)

    if overview: 
        overview.close()

if __name__ == "__main__":
    main()

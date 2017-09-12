import os
import errno
import time
import json
import yaml
import boto3
import dulwich
import shutil
from cStringIO import StringIO
from unidiff import PatchSet
from dulwich import porcelain 
from dulwich.contrib.paramiko_vendor import ParamikoSSHVendor
from botocore.exceptions import ClientError

REGION = None
DRYRUN = None
GIT_REPO = None
SSH_KEY_PATH = None
SYSTEM_PARAM_PREFIX = None
PARAM_PREFIX = None
SNS_TOPIC_ARN = None
PATH_TO_REPO = "/tmp/repo"

def initialize():
    global REGION
    global DRYRUN
    global GIT_REPO
    global SSH_KEY_PATH
    global SYSTEM_PARAM_PREFIX
    global PARAM_PREFIX
    global SNS_TOPIC_ARN

    PARAM_PREFIX = os.environ.get("PARAM_PREFIX")
    SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN", None)
    GIT_REPO = os.environ.get("GIT_REPO")
    SYSTEM_PARAM_PREFIX = os.environ.get("SYSTEM_PARAM_PREFIX")
    REGION = os.environ.get('REGION', "None")
    DRYRUN = os.environ.get('DRYRUN', "true").lower()
    SSH_KEY_PATH = os.environ.get("SSH_KEY_PATH", None)

    if DRYRUN == "false":
        DRYRUN = False
    else:
        DRYRUN = True

    ## cleanup repo if it exist by some reason
    shutil.rmtree(PATH_TO_REPO, ignore_errors=True)

# if SSH_KEY_PATH is not set, then trying to read one from
# ec2 param "SYSTEM_PARAM_PREFIX/ssh-key" 
def set_up_ssh_key(ssm):
    global SSH_KEY_PATH
    if SSH_KEY_PATH is None:
        SSH_KEY_PATH = '/tmp/id_rsa'
    
        response = ssm.get_parameter(
            Name=os.path.join(SYSTEM_PARAM_PREFIX, "ssh-key"),
            WithDecryption=True,
        )

        if 'Parameter' in response and 'Value' in response['Parameter']:
            with open(SSH_KEY_PATH, "w") as text_file:
                text_file.write(response['Parameter']['Value'])


def clone_or_pull_repo(git_repo, path_to_repo):
    dulwich.client.get_ssh_vendor = KeyParamikoSSHVendor
    try:
        # clonning git repo
        repo = dulwich.porcelain.clone(git_repo, path_to_repo)
    except OSError as e:
        if e.errno == errno.EEXIST:
            repo = dulwich.porcelain.open_repo(path_to_repo)
            # pulling changes for existing repo
            dulwich.porcelain.pull(repo, git_repo)
        else:
            raise e

    return repo


# list existing parameters in the ec2 param store by given prefix
## this method currently is not used
def get_existing_parameters(ssm, prefix):
    parameters = []
    is_in = True

    req = {'Filters': [{'Key': 'Name', 'Values': [prefix]}], 'MaxResults': 50}

    while is_in:
        start_time = time.time()
        response = ssm.describe_parameters(**req)
        if 'Parameters' in response:
            parameters += response['Parameters']

        if 'NextToken' in response:
            req['NextToken'] = response['NextToken']

        is_in = 'NextToken' in response and response['NextToken']
        print("ExistingParams iteration time", time.time() - start_time)
    return parameters

# get latest commit info for 
# * repo - when f=None
# * file - when f=[file]
def get_latest_commit(repo, f=None):
    w = repo.get_walker(paths=f, max_entries=1)
    try:
        c = iter(w).next().commit
    except StopIteration:
        print("No file {} anywhere in history.".format(f))
    else:
        return c

# Check difference between 2 commits and return
# lists of added_files, modified_files and removed_files
def diff_revisions(repo, commit1, commit2):
    print("Comparing commits {} and {}".format(commit1, commit2))

    diff_stream= StringIO()
    porcelain.diff_tree(repo, repo[commit1.encode('ascii')].tree,repo[commit2.encode('ascii')].tree, outstream=diff_stream)

    patch = PatchSet.from_string(diff_stream.getvalue())
    diff_stream.close()

    # geting added/modified file name from the diff, by getting "target_file" and stripping "a/" prefix
    # (source file name will be /dev/null)
    added_files = [f.target_file[2:]  for f in patch.added_files]
    modified_files = [f.target_file[2:]  for f in patch.modified_files]
    # geting removed files names from the diff, by getting "source_file" and stripping "b/" prefix
    # (target file name will be /dev/null)
    removed_files = [f.source_file[2:]  for f in patch.removed_files]

    return added_files, modified_files, removed_files

# list all files in the directory
# excluding some dirs and files like:
# .git, .gitingore, etc
def list_dir(path):
    files = []

    for dirname, dirnames, filenames in os.walk(path):
        if '.git' in dirnames:
            # don't go into any .git directories.
            dirnames.remove('.git')

        if '.gitignore' in filenames:
            filenames.remove('.gitignore')
        elif 'README.md' in filenames:
            filenames.remove('README.md')

        # print path to all filenames.
        for filename in filenames:
            files.append(os.path.join(dirname, filename))

    print("Found next files:")
    for f in files: print(f)
    print

    return files


def validate_format(file, filecontent):
    name, ext = os.path.splitext(file)
    if ext == ".json":
        try:
            json.loads(filecontent)
        except ValueError as exc:
            return "JSON format problem: {}".format(str(exc))
        return None
    elif ext == ".yml" or ext == ".yaml":
        try:
            yaml.load(filecontent)
        except yaml.YAMLError as exc:
            return "YAML format problem: {}".format(str(exc))
        return None


# find latest revision of the file and upload
# it to the ec2 parameters
# with:
# Name - filepath
# Value - file content
# Description - latest commit id
def upload_as_parameters(ssm, repo, files):
    uploaded = []
    failed = []
    for f in files:
        start_time = time.time()
        # Param config name should start with the "/"
        params_file = os.path.join(PARAM_PREFIX, f)

        # getting latest commit for specified file
        c = get_latest_commit(repo, f=[f])

        # Update param only if its Description differs from latest commit
        if not DRYRUN:
            update_msg = {"Key":params_file, "Commit":c.id, "Author":c.author, "Time":time.ctime(c.author_time)}

            # reading content of the file
            with open(os.path.join(repo.path,f), 'r') as myfile:
                data = myfile.read()

            err = validate_format(f, data)
            if err is not None:
                update_msg['Error'] = err
                failed.append(update_msg)
                print("ERROR: Problem validating file format. File: {}. Details: {}".format(params_file, err))
                continue

            print("Updating param {}".format(params_file))
            try:
                response = ssm.put_parameter(
                    Name=params_file,
                    Description=c.id,
                    Value=data,
                    Type='SecureString',
                    # KeyId='string',
                    Overwrite=True,
                    # AllowedPattern='string'
                )
                uploaded.append(update_msg)
            except Exception as e:
                update_msg['Error'] = "Upload problem: {}".format(e)
                failed.append(update_msg)
                print("ERROR: Couldn't update param {}. Details: {}".format(params_file, e))
        else:
            print("Skipping param update for {}".format(params_file))
        print("Upload iteration time", time.time() - start_time)
        print
    
    return uploaded, failed

# call to delete ec2 parameters
def delete_parameters(ssm, files):
    if len(files) == 0:
        return None, None

    # getting filename from PatchFile object and converting
    # to the array with ec2 params names PREFIX/file
    params_files = [os.path.join(PARAM_PREFIX, f) for f in files]
     
    if not DRYRUN:
        try: 
            response = ssm.delete_parameters(
                Names=params_files
            )
        except Exception as e :
            print("ERROR: deleting params: {}".format(e))
        
        print("Deleting params: {}".format(params_files))
        return response['DeletedParameters'], response['InvalidParameters']
    else:
        print("Skipping deletion for params: {}".format(params_files))

    return None, None

# getting latest revision id from 
# ec2 param "SYSTEM_PARAM_PREFIX/revision"
def get_latest_processed_revision(ssm):
    try:
        # geting latest processed commit id so we can run a diff
        response = ssm.get_parameter(
            Name=os.path.join(SYSTEM_PARAM_PREFIX, "revision"),
            WithDecryption=True,
        )
        if 'Parameter' in response and 'Value' in response['Parameter']:
            return response['Parameter']['Value']
    except ClientError as e:
        if e.response['Error']['Code'] != 'ParameterNotFound':
            raise e

    return None

# send SNS messages if function did some upload/removal
# TODO: send sns messages with errors if something went wrong
def send_sns_notification(msg):
    print(msg)
    if SNS_TOPIC_ARN is not None:
        # checking if msg contain any data in it
        if any(lst for v in msg.values() if isinstance(v, dict) for lst in v.values()):
            sns = boto3.client('sns', region_name=REGION)
            # Pushing message to SNS, which will be pushed to hipchat by other lambda function
            sns.publish(
                TargetArn=SNS_TOPIC_ARN,
                Message=json.dumps({'default': json.dumps(msg)}),
                MessageStructure='json',
            )

# needed for specifying custom ssh key for paramiko ssh
class KeyParamikoSSHVendor(ParamikoSSHVendor):  
    def __init__(self):
        self.ssh_kwargs = {'key_filename': SSH_KEY_PATH}

def lambda_handler(event, context):
    # initializaing ENV variales
    initialize()

    # prepare object with the messages that going to
    # be sent to the sns.
    # setting "type" key, so we can easier identify message 
    # in the sns2slack lambda
    msg = {'type': 'git2params'}

    ssm = boto3.client('ssm', region_name=REGION)

    # configuring ssh key for git client
    set_up_ssh_key(ssm)

    # clonning the git repository
    repo = clone_or_pull_repo(GIT_REPO, PATH_TO_REPO)

    # geting latest saved revision id from the param store
    latest_processed_commit = get_latest_processed_revision(ssm)

    # gate latest commit repo wide
    latest_commit = get_latest_commit(repo)

    if latest_processed_commit == latest_commit.id:
        print("No new commits found. Exiting")
        return {'statusCode': 200}

    # if latest processed commit not found, then treat execution
    # like first run (adding new keys and overwriting existing)
    if latest_processed_commit is None:
        # listing files in the git repository
        files = list_dir(PATH_TO_REPO)
        msg['added'] = {}
        msg['added']['success'], msg['added']['errors'] = upload_as_parameters(
            ssm,
            repo,
            [os.path.relpath(f, PATH_TO_REPO) for f in files]
        )
    else:
        # getting diff of the current and latest processed revisions and:
        # * uploading added files
        # * uploading modified files
        # * deleting removed files
        added_files, modified_files, removed_files = diff_revisions(repo, latest_processed_commit, latest_commit.id)
        if added_files:
            msg['added'] = {}
            msg['added']['success'], msg['added']['errors']= upload_as_parameters(
                ssm,
                repo,
                added_files
            )
        if modified_files:
            msg['modified'] = {}
            msg['modified']['success'], msg['modified']['errors'] = upload_as_parameters(
                ssm,
                repo,
                modified_files
            )
        if removed_files:
            msg['removed'] = {}
            msg['removed']['success'], msg['removed']['errors'] = delete_parameters(
                ssm,
                removed_files
            )

    if not DRYRUN:
        # uploading latest revision id to the ec2parans
        latest_revision_key = os.path.join(SYSTEM_PARAM_PREFIX, "revision")
        print("saving latest revision {} to the key {}".format(latest_commit.id,latest_revision_key))        
        response = ssm.put_parameter(
            Name=latest_revision_key,
            Description="Latest pulled commit",
            Value=latest_commit.id,
            Type='SecureString',
            # KeyId='string',
            Overwrite=True,
            # AllowedPattern='string'
        )

        #sending message to the sns
        send_sns_notification(msg)
    else:
        print("Latest revision {}".format(latest_commit.id))

    return {'statusCode': 200}

if __name__ == '__main__':
    lambda_handler(None, None)

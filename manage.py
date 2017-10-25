#! /usr/bin/env python

import click
import boto3
import os
import json
import py_compile
import shutil
import pip
import hashlib
from zipfile import ZipInfo, ZipFile, ZIP_DEFLATED

# ENVIRONMENTAL VARIABLES

S3_BUCKET="kinja-cloudformation-validator"
CLOUDFORMATION_LOCAL_VALIDATE_LIMIT=51200
DEFAULT_BUNDLE_DIRECTORY="build"
CONFIGURATION_DIRECTORY="configs"
CLOUDFORMATION_DIRECTORY='cloudformation'
LAMBDA_DIRECTORY='lambdas'
LAMBDA_PY_FILE_NAME='function.py'
LAMBDA_REQUIRED_FILES=[LAMBDA_PY_FILE_NAME, 'requirements.txt']
TMP_DIR=".tmp"

# UTILS
def _copytree(src, dst, symlinks=False):
    names = os.listdir(src)
    os.makedirs(dst)
    errors = []
    for name in names:
        srcname = os.path.join(src, name)
        dstname = os.path.join(dst, name)
        try:
            if symlinks and os.path.islink(srcname):
                linkto = os.readlink(srcname)
                os.symlink(linkto, dstname)
            elif os.path.isdir(srcname):
                _copytree(srcname, dstname, symlinks)
            else:
                shutil.copy2(srcname, dstname)
                shutil.copystat(srcname, dstname)
            # XXX What about devices, sockets etc.?
        except OSError as why:
            errors.append((srcname, dstname, str(why)))
        # catch the Error from the recursive copytree so that we can
        # continue with other files
        except shutil.Error as err:
            errors.extend(err.args[0])
    try:
        shutil.copystat(src, dst)
    except OSError as why:
        # can't copy file access times on Windows
        if why.winerror is None:
            errors.extend((src, dst, str(why)))
    if errors:
        raise shutil.Error(errors)

def _walklevel(some_dir, level=1):
    some_dir = some_dir.rstrip(os.path.sep)
    assert os.path.isdir(some_dir)
    num_sep = some_dir.count(os.path.sep)
    for root, dirs, files in os.walk(some_dir):
        yield root, dirs, files
        num_sep_this = root.count(os.path.sep)
        if num_sep + level <= num_sep_this:
            del dirs[:]

def _list_files(directory, extension):
    files = []
    for filename in os.listdir(directory):
        if filename.endswith(extension):
            template = os.path.join(directory, filename)
            files.append(template)
    return files

def _list_directories(directory):
    return [dir[0] for dir in _walklevel(directory) if dir[0] != directory]

def _run_for_each(seq, method):
    ret = []
    for obj in seq:
         ret.append(method(obj))
    return ret

def _install_pip_requirements(path, target):
    pip.main(['install', '-r', path, '-t', target])

def _zip_lambda_bundle(source, destination):
    # Zip lambda package, forcing all permissions for each file
    with ZipFile(destination + '.zip', 'w', compression=ZIP_DEFLATED) as zip_file:

        for root, dirs, files in os.walk(source):
            for file in files:
                file_name = os.path.join(root, file)

                zip_info = ZipInfo(os.path.relpath(file_name, source))  # Preserve relative path
                zip_info.compress_type = ZIP_DEFLATED
                zip_info.create_system = 3  # Specifies Unix
                # mparent(2017-07-12): Forcing all permissions was the only way I could allow
                # packaged binaries (ex: pg_dump) to run inside lambda without "permission denied"
                # errors.
                zip_info.external_attr = 0777 << 16L  # NOQA

                zip_file.writestr(zip_info, open(file_name).read())  # You have to write the file contents in with ZipInfo


def _get_file_size(path):
    statinfo = os.stat(path)
    return statinfo.st_size

def _upload_to_s3(path, bucket, key):
    s3 = boto3.client('s3')
    with open(path, 'rb') as data:
        s3.upload_fileobj(data, bucket, key)

def _render_configuration(source, destination, fields):
    with open(source) as data_file:
        data = json.load(data_file)
    location=os.path.join(destination, os.path.basename(source))
    for k,v in fields.iteritems():
        data['Parameters'][k] = v
    with open(location, 'w') as outfile:
            json.dump(data, outfile)

# VALIDATOR PRIMITIVES

def validate_local_cloudformation_template(path):
    click.echo("Validating (local)... [%s]" % path)
    client = boto3.client('cloudformation')
    response=""
    try:
        with open(path, 'r') as template_file:
                    body=template_file.read()
                    response = client.validate_template(
                        TemplateBody=body
                    )
    except:
        click.echo("...FAIL...")
        raise
    click.echo("...PASS...")
    return response

def validate_remote_cloudformation_template(path):
    click.echo("Validating (remote)... [%s]" % path)

    key=hashlib.md5(path).hexdigest()
    _upload_to_s3(path, S3_BUCKET, key)

    client = boto3.client('cloudformation')
    try:
       location = 'https://s3.amazonaws.com/' + S3_BUCKET + '/' + key
       client.validate_template(TemplateURL=location)
    except:
        click.echo("...FAIL...")
        raise
    click.echo("...PASS...")

def validate_cloudformation_template_file(path):
    if _get_file_size(path) <= CLOUDFORMATION_LOCAL_VALIDATE_LIMIT:
        return validate_local_cloudformation_template(path)
    else:
        return validate_remote_cloudformation_template(path)

def validate_python_file(path):
    click.echo("Compiling python file... [%s]" % path)
    py_compile.compile(path, doraise=True)
    os.remove(path+'c') # removing pyc files

def validate_lambda_bundle(path):
    click.echo("Validating... [%s]" % path)
    if not os.path.isdir(path):
        raise Exception("%s is not a directory..." % path)
    for file in LAMBDA_REQUIRED_FILES:
        filepath = os.path.join(path, file)
        if not os.path.isfile(filepath):
            raise Exception("%s is missing..." % filepath)
    py_file_path = os.path.join(path, LAMBDA_PY_FILE_NAME)
    validate_python_file(py_file_path)
    click.echo("...PASS...")

def validate_configuration_file(path):
    click.echo("Validating... [%s]" % path)
    with open(path) as config_file:
        json.load(config_file)
    click.echo("...PASS...")

# ENVIRONMENT VALIDATORS

def validate_cloudformation_templates(filename=None):
    click.echo("Validating cloudformation templates...")
    templates = _list_files(CLOUDFORMATION_DIRECTORY, ".yaml") if not filename else [filename]
    _run_for_each(templates, validate_cloudformation_template_file)

def validate_configuration_files(filename=None):
    click.echo("Validating configuration files...")
    configurations = _list_files(CONFIGURATION_DIRECTORY, ".json") if not filename else [filename]
    _run_for_each(configurations, validate_configuration_file)

def validate_lambda_bundles(directory=None):
    click.echo("Validating lambda bundles...")
    bundles = _list_directories(LAMBDA_DIRECTORY) if not directory else [directory]
    _run_for_each(bundles, validate_lambda_bundle)

# BUNDLE GENERATORS

def create_deployment_bundle(directory, validate, initiator, id):
    click.echo("Creating deployment bundle...")

    if validate:
        validate_cloudformation_templates()
        validate_lambda_bundles()
        validate_configuration_files()

    if os.path.exists(directory):
      shutil.rmtree(directory)

    if os.path.exists(TMP_DIR):
      shutil.rmtree(TMP_DIR)

    os.mkdir(TMP_DIR)
    os.mkdir(directory)

    # LAMBDA
    _copytree(LAMBDA_DIRECTORY, os.path.join(TMP_DIR, LAMBDA_DIRECTORY))
    os.mkdir(os.path.join(directory, LAMBDA_DIRECTORY))
    lambdas = _list_directories(os.path.join(TMP_DIR, LAMBDA_DIRECTORY))
    for bundle in lambdas:
        _install_pip_requirements(os.path.join(bundle, 'requirements.txt'), bundle)
        _zip_lambda_bundle(
            source=bundle,
            destination=os.path.join(directory, LAMBDA_DIRECTORY, os.path.basename(bundle))
        )

    # CLOUDFORMATION
    _copytree(CLOUDFORMATION_DIRECTORY, os.path.join(directory, CLOUDFORMATION_DIRECTORY))

    # CONFIGURATION
    os.mkdir(os.path.join(directory, CONFIGURATION_DIRECTORY))
    GENERATED_FIELDS = {
        'StackConfigurationBucket': initiator,
        'StackBuildId': id
    }

    configurations = _list_files(CONFIGURATION_DIRECTORY, ".json")
    for config in configurations:
        _render_configuration(config, os.path.join(directory, CONFIGURATION_DIRECTORY), GENERATED_FIELDS)

    shutil.rmtree(TMP_DIR)

    click.echo("Bundle has been created...")


# COMMANDLINE DEFINITIONS

@click.group()
def cli():
    pass

@cli.command(help='validate cloudformation templates or a specific template file')
@click.option('--filename', default=None, help='path to the cloudformation template to be validated')
def validate_cloudformation(filename):
    validate_cloudformation_templates(filename)

@cli.command(help='validate cloudformation configuration file')
@click.option('--filename', default=None, help='path to the configuration file to be validated')
def validate_configuration(filename):
    validate_configuration_files(filename)

@cli.command(help='validate lambda bundle directory')
@click.option('--directory', default=None, help='path to the lambda bundle directory')
def validate_lambda(directory):
    validate_lambda_bundle(directory)

@cli.command(help='run validation tests')
def validate():
    validate_cloudformation_templates()
    validate_lambda_bundles()
    validate_configuration_files()

@cli.command(help='create deployment bundle')
@click.option('--directory', default=DEFAULT_BUNDLE_DIRECTORY, help='deployment bundle directory location')
@click.option('--initiator', default="USER", help='name of the deployment initiator')
@click.option('--id', default="USER", help='name of the deployment id')
@click.option('--validate/--no-validate', default=True, help='run validation before creating the bundle')
def create_bundle(directory, validate, initiator, id):
    create_deployment_bundle(directory, validate, initiator, id)

if __name__ == '__main__':
    try:
        cli()
    except Exception as e:
        click.echo(str(e))
        exit(-1)

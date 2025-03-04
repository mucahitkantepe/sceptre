from ast import literal_eval
from copy import deepcopy
from io import StringIO
from pathlib import Path
from typing import Type

from behave import *
import time
import os
import yaml
import boto3
from contextlib import contextmanager
from botocore.exceptions import ClientError
from helpers import read_template_file, get_cloudformation_stack_name
from helpers import retry_boto_call
from sceptre.diffing.diff_writer import DeepDiffWriter, DiffWriter
from sceptre.diffing.stack_differ import DeepDiffStackDiffer, DifflibStackDiffer, StackDiff

from sceptre.plan.plan import SceptrePlan
from sceptre.context import SceptreContext


def set_stack_timeout(context, stack_name, stack_timeout):
    config_path = os.path.join(
        context.sceptre_dir, "config", stack_name + ".yaml"
    )
    with open(config_path) as config_file:
        stack_config = yaml.safe_load(config_file)

    stack_config["stack_timeout"] = int(stack_timeout)

    with open(config_path, 'w') as config_file:
        yaml.safe_dump(stack_config, config_file, default_flow_style=False)


@given('stack "{stack_name}" does not exist')
def step_impl(context, stack_name):
    full_name = get_cloudformation_stack_name(context, stack_name)
    status = get_stack_status(context, full_name)
    if status is not None:
        delete_stack(context, full_name)
    status = get_stack_status(context, full_name)
    assert (status is None)


@given('stack "{stack_name}" does not exist in "{region_name}"')
def step_impl(context, stack_name, region_name):
    full_name = get_cloudformation_stack_name(context, stack_name)
    with region(region_name):
        status = get_stack_status(context, full_name)
        if status is not None:
            delete_stack(context, full_name)
        status = get_stack_status(context, full_name)
    assert (status is None)


@given('stack "{stack_name}" exists in "{desired_status}" state')
def step_impl(context, stack_name, desired_status):
    full_name = get_cloudformation_stack_name(context, stack_name)

    status = get_stack_status(context, full_name)
    if status != desired_status:
        delete_stack(context, full_name)
        if desired_status == "CREATE_COMPLETE":
            body = read_template_file(context, "valid_template.json")
            create_stack(context, full_name, body)
        elif desired_status == "CREATE_FAILED":
            body = read_template_file(context, "invalid_template.json")
            kwargs = {"OnFailure": "DO_NOTHING"}
            create_stack(context, full_name, body, **kwargs)
        elif desired_status == "UPDATE_COMPLETE":
            body = read_template_file(context, "valid_template.json")
            create_stack(context, full_name, body)
            body = read_template_file(context, "updated_template.json")
            update_stack(context, full_name, body)
        elif desired_status == "ROLLBACK_COMPLETE":
            body = read_template_file(context, "invalid_template.json")
            kwargs = {"OnFailure": "ROLLBACK"}
            create_stack(context, full_name, body, **kwargs)

    status = get_stack_status(context, full_name)
    assert (status == desired_status)


@given('stack "{stack_name}" exists using "{template_name}"')
def step_impl(context, stack_name, template_name):
    full_name = get_cloudformation_stack_name(context, stack_name)

    status = get_stack_status(context, full_name)
    if status != "CREATE_COMPLETE":
        delete_stack(context, full_name)
        body = read_template_file(context, template_name)
        create_stack(context, full_name, body)

    status = get_stack_status(context, full_name)
    assert (status == "CREATE_COMPLETE")


@given('the stack_timeout for stack "{stack_name}" is "{stack_timeout}"')
def step_impl(context, stack_name, stack_timeout):
    set_stack_timeout(context, stack_name, stack_timeout)


@given('stack "{dependant_stack_name}" depends on stack "{stack_name}"')
def step_impl(context, dependant_stack_name, stack_name):
    sceptre_context = SceptreContext(
        command_path=stack_name + '.yaml',
        project_path=context.sceptre_dir
    )
    plan = SceptrePlan(sceptre_context)
    plan.resolve('create')
    if plan.launch_order:
        for stack in plan.launch_order:
            stk = stack.pop()
            if stk.name == stack_name:
                for d in stk.dependencies:
                    if d.name == dependant_stack_name:
                        assert True
                        return
    assert False


@given('the stack config for stack "{stack_name}" has changed')
def step_impl(context, stack_name):
    sceptre_context = SceptreContext(
        command_path=stack_name + '.yaml',
        project_path=context.sceptre_dir,
        ignore_dependencies=True
    )
    yaml_file = Path(sceptre_context.full_config_path()) / f'{stack_name}.yaml'
    with yaml_file.open(mode='r') as f:
        loaded = yaml.load(f)

    original_config = deepcopy(loaded)
    loaded['stack_tags'] = {
        'NewTag': 'NewValue'
    }
    dump_stack_config(yaml_file, loaded)

    context.add_cleanup(dump_stack_config, yaml_file, original_config)


def dump_stack_config(config_path: Path, config_dict: dict):
    with config_path.open(mode='w') as f:
        yaml.safe_dump(config_dict, f)


@when('the user creates stack "{stack_name}"')
def step_impl(context, stack_name):
    sceptre_context = SceptreContext(
        command_path=stack_name + '.yaml',
        project_path=context.sceptre_dir
    )

    sceptre_plan = SceptrePlan(sceptre_context)
    try:
        sceptre_plan.create()
    except ClientError as e:
        if e.response['Error']['Code'] == 'AlreadyExistsException' \
                and e.response['Error']['Message'].endswith("already exists"):
            return
        else:
            raise e


@when('the user creates stack "{stack_name}" with ignore dependencies')
def step_impl(context, stack_name):
    sceptre_context = SceptreContext(
        command_path=stack_name + '.yaml',
        project_path=context.sceptre_dir,
        ignore_dependencies=True
    )

    sceptre_plan = SceptrePlan(sceptre_context)
    try:
        sceptre_plan.create()
    except ClientError as e:
        if e.response['Error']['Code'] == 'AlreadyExistsException' \
                and e.response['Error']['Message'].endswith("already exists"):
            return
        else:
            raise e


@when('the user updates stack "{stack_name}"')
def step_impl(context, stack_name):
    sceptre_context = SceptreContext(
        command_path=stack_name + '.yaml',
        project_path=context.sceptre_dir
    )

    sceptre_plan = SceptrePlan(sceptre_context)
    try:
        sceptre_plan.update()
    except ClientError as e:
        message = e.response['Error']['Message']
        if e.response['Error']['Code'] == 'ValidationError' \
                and message.endswith("does not exist"):
            return
        else:
            raise e


@when('the user updates stack "{stack_name}" with ignore dependencies')
def step_impl(context, stack_name):
    sceptre_context = SceptreContext(
        command_path=stack_name + '.yaml',
        project_path=context.sceptre_dir,
        ignore_dependencies=True
    )

    sceptre_plan = SceptrePlan(sceptre_context)
    try:
        sceptre_plan.update()
    except ClientError as e:
        message = e.response['Error']['Message']
        if e.response['Error']['Code'] == 'ValidationError' \
                and message.endswith("does not exist"):
            return
        else:
            raise e


@when('the user deletes stack "{stack_name}"')
def step_impl(context, stack_name):
    sceptre_context = SceptreContext(
        command_path=stack_name + '.yaml',
        project_path=context.sceptre_dir,
        full_scan=True
    )

    sceptre_plan = SceptrePlan(sceptre_context)
    sceptre_plan.resolve(command='delete', reverse=True)

    try:
        sceptre_plan.delete()
    except ClientError as e:
        if e.response['Error']['Code'] == 'ValidationError' \
                and e.response['Error']['Message'].endswith("does not exist"):
            return
        else:
            raise e


@when('the user deletes stack "{stack_name}" with ignore dependencies')
def step_impl(context, stack_name):
    sceptre_context = SceptreContext(
        command_path=stack_name + '.yaml',
        project_path=context.sceptre_dir,
        ignore_dependencies=True,
        full_scan=True
    )

    sceptre_plan = SceptrePlan(sceptre_context)
    sceptre_plan.resolve(command='delete', reverse=True)

    try:
        sceptre_plan.delete()
    except ClientError as e:
        if e.response['Error']['Code'] == 'ValidationError' \
                and e.response['Error']['Message'].endswith("does not exist"):
            return
        else:
            raise e


@when('the user launches stack "{stack_name}"')
def step_impl(context, stack_name):
    sceptre_context = SceptreContext(
        command_path=stack_name + '.yaml',
        project_path=context.sceptre_dir
    )

    sceptre_plan = SceptrePlan(sceptre_context)

    try:
        sceptre_plan.launch()
    except Exception as e:
        context.error = e


@when('the user launches stack "{stack_name}" with ignore dependencies')
def step_impl(context, stack_name):
    sceptre_context = SceptreContext(
        command_path=stack_name + '.yaml',
        project_path=context.sceptre_dir,
        ignore_dependencies=True
    )

    sceptre_plan = SceptrePlan(sceptre_context)

    try:
        sceptre_plan.launch()
    except Exception as e:
        context.error = e


@when('the user describes the resources of stack "{stack_name}"')
def step_impl(context, stack_name):
    sceptre_context = SceptreContext(
        command_path=stack_name + '.yaml',
        project_path=context.sceptre_dir
    )

    sceptre_plan = SceptrePlan(sceptre_context)
    context.output = list(sceptre_plan.describe_resources().values())


@when('the user describes the resources of stack "{stack_name}" with ignore dependencies')
def step_impl(context, stack_name):
    sceptre_context = SceptreContext(
        command_path=stack_name + '.yaml',
        project_path=context.sceptre_dir,
        ignore_dependencies=True
    )

    sceptre_plan = SceptrePlan(sceptre_context)
    context.output = list(sceptre_plan.describe_resources().values())


@when('the user diffs stack "{stack_name}" with "{diff_type}"')
def step_impl(context, stack_name, diff_type):
    sceptre_context = SceptreContext(
        command_path=stack_name + '.yaml',
        project_path=context.sceptre_dir
    )
    sceptre_plan = SceptrePlan(sceptre_context)
    differ_classes = {
        'deepdiff': DeepDiffStackDiffer,
        'difflib': DifflibStackDiffer
    }
    writer_class = {
        'deepdiff': DeepDiffWriter,
        'difflib': DeepDiffWriter
    }

    differ = differ_classes[diff_type]()
    context.writer_class = writer_class[diff_type]
    context.output = list(sceptre_plan.diff(differ).values())


@then(
    'stack "{stack_name}" in "{region_name}" '
    'exists in "{desired_status}" state'
)
def step_impl(context, stack_name, region_name, desired_status):
    with region(region_name):
        full_name = get_cloudformation_stack_name(context, stack_name)
        status = get_stack_status(context, full_name, region_name)

        assert (status == desired_status)


@then('stack "{stack_name}" exists in "{desired_status}" state')
def step_impl(context, stack_name, desired_status):
    full_name = get_cloudformation_stack_name(context, stack_name)
    sceptre_context = SceptreContext(
        command_path=stack_name + '.yaml',
        project_path=context.sceptre_dir
    )

    sceptre_plan = SceptrePlan(sceptre_context)
    status = sceptre_plan.get_status()
    status = get_stack_status(context, full_name)
    assert (status == desired_status)


@then('stack "{stack_name}" has "{tag_name}" tag with "{desired_tag_value}" value')
def step_impl(context, stack_name, tag_name, desired_tag_value):
    full_name = get_cloudformation_stack_name(context, stack_name)

    tags = get_stack_tags(context, full_name)
    tag = next((tag for tag in tags if tag['Key'] == tag_name), {'Value': None})

    assert (tag['Value'] == desired_tag_value)


@then('stack "{stack_name}" does not exist')
def step_impl(context, stack_name):
    full_name = get_cloudformation_stack_name(context, stack_name)
    status = get_stack_status(context, full_name)
    assert (status is None)


@then('the resources of stack "{stack_name}" are described')
def step_impl(context, stack_name):
    full_name = get_cloudformation_stack_name(context, stack_name)
    response = retry_boto_call(
        context.client.describe_stack_resources,
        StackName=full_name
    )
    properties = {"LogicalResourceId", "PhysicalResourceId"}
    formatted_response = [
        {k: v for k, v in item.items() if k in properties}
        for item in response["StackResources"]
    ]

    assert [{stack_name: formatted_response}] == context.output


@then('stack "{stack_name}" does not exist and stack "{dependant_stack_name}" exists in "{desired_state}"')
def step_impl(context, stack_name, dependant_stack_name, desired_state):
    full_name = get_cloudformation_stack_name(context, stack_name)
    status = get_stack_status(context, full_name)
    assert (status is None)

    dep_full_name = get_cloudformation_stack_name(context, dependant_stack_name)
    sceptre_context = SceptreContext(
        command_path=stack_name + '.yaml',
        project_path=context.sceptre_dir
    )

    sceptre_plan = SceptrePlan(sceptre_context)
    dep_status = sceptre_plan.get_status()
    dep_status = get_stack_status(context, dep_full_name)
    assert (dep_status == desired_state)


@then('a diff is returned with "{attribute}" = "{value}"')
def step_impl(context, attribute, value):
    for diff in context.output:
        expected_value = literal_eval(value)
        actual_value = getattr(diff, attribute)
        assert actual_value == expected_value


@then('a diff is returned with {a_or_no} "{kind}" difference')
def step_impl(context, a_or_no, kind):
    if a_or_no == 'a':
        test_value = True
    elif a_or_no == 'no':
        test_value = False
    else:
        raise ValueError('Only "a" or "no" accepted in this condition')

    writer_class: Type[DiffWriter] = context.writer_class
    difference_property = f'has_{kind}_difference'

    for diff in context.output:
        diff: StackDiff
        writer = writer_class(diff, StringIO(), 'yaml')
        assert getattr(writer, difference_property) is test_value


def get_stack_tags(context, stack_name, region_name=None):
    if region_name is not None:
        stack = boto3.resource('cloudformation', region_name=region_name).Stack
    else:
        stack = context.cloudformation.Stack

    try:
        stack = retry_boto_call(stack, stack_name)
        retry_boto_call(stack.load)
        return stack.tags
    except ClientError as e:
        if e.response['Error']['Code'] == 'ValidationError' \
                and e.response['Error']['Message'].endswith("does not exist"):
            return None
        else:
            raise e


def get_stack_status(context, stack_name, region_name=None):
    if region_name is not None:
        Stack = boto3.resource('cloudformation', region_name=region_name).Stack
    else:
        Stack = context.cloudformation.Stack

    try:
        stack = retry_boto_call(Stack, stack_name)
        retry_boto_call(stack.load)
        return stack.stack_status
    except ClientError as e:
        if e.response['Error']['Code'] == 'ValidationError' \
                and e.response['Error']['Message'].endswith("does not exist"):
            return None
        else:
            raise e


def create_stack(context, stack_name, body, **kwargs):
    retry_boto_call(
        context.client.create_stack,
        StackName=stack_name,
        TemplateBody=body, **kwargs,
        Capabilities=[
            'CAPABILITY_IAM',
            'CAPABILITY_NAMED_IAM',
            'CAPABILITY_AUTO_EXPAND'
        ]
    )

    wait_for_final_state(context, stack_name)


def update_stack(context, stack_name, body, **kwargs):
    stack = retry_boto_call(context.cloudformation.Stack, stack_name)
    retry_boto_call(stack.update, TemplateBody=body, **kwargs)

    wait_for_final_state(context, stack_name)


def delete_stack(context, stack_name):
    stack = retry_boto_call(context.cloudformation.Stack, stack_name)
    retry_boto_call(stack.delete)

    waiter = context.client.get_waiter('stack_delete_complete')
    waiter.config.delay = 5
    waiter.config.max_attempts = 240
    waiter.wait(StackName=stack_name)


@contextmanager
def region(region_name):
    os.environ["AWS_REGION"] = region_name
    yield
    del os.environ["AWS_REGION"]


def wait_for_final_state(context, stack_name):
    stack = retry_boto_call(context.cloudformation.Stack, stack_name)
    delay = 2
    max_retries = 150
    attempts = 0
    while attempts < max_retries:
        retry_boto_call(stack.load)
        if not stack.stack_status.endswith("IN_PROGRESS"):
            return
        attempts += 1
        time.sleep(delay)
    raise Exception("Timeout waiting for stack to reach final state.")

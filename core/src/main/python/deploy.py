"""
Copyright (c) 2017, 2020, Oracle Corporation and/or its affiliates.
Licensed under the Universal Permissive License v 1.0 as shown at https://oss.oracle.com/licenses/upl.

The entry point for the deployApps tool.
"""
import os
import sys

from java.io import PrintStream
from java.lang import System


from oracle.weblogic.deploy.deploy import DeployException
from oracle.weblogic.deploy.exception import BundleAwareException
from oracle.weblogic.deploy.util import CLAException
from oracle.weblogic.deploy.util import WebLogicDeployToolingVersion

sys.path.append(os.path.dirname(os.path.realpath(sys.argv[0])))

# imports from local packages start here
from wlsdeploy.aliases.aliases import Aliases
from wlsdeploy.aliases.wlst_modes import WlstModes
from wlsdeploy.exception.expection_types import ExceptionType
from wlsdeploy.logging.platform_logger import PlatformLogger
from wlsdeploy.tool.deploy import deployer_utils
from wlsdeploy.tool.deploy import model_deployer
from wlsdeploy.tool.util import model_context_helper
from wlsdeploy.tool.util.string_output_stream import StringOutputStream
from wlsdeploy.tool.util import wlst_helper
from wlsdeploy.tool.util.wlst_helper import WlstHelper
from wlsdeploy.util import cla_helper
from wlsdeploy.util import tool_exit
from wlsdeploy.util.cla_utils import CommandLineArgUtil
from wlsdeploy.util.model import Model
from wlsdeploy.util.weblogic_helper import WebLogicHelper

wlst_helper.wlst_functions = globals()


_program_name = 'deployApps'
_class_name = 'deploy'
__logger = PlatformLogger('wlsdeploy.deploy')
__wls_helper = WebLogicHelper(__logger)
__wlst_helper = WlstHelper(ExceptionType.DEPLOY)
__wlst_mode = WlstModes.OFFLINE

__required_arguments = [
    CommandLineArgUtil.ORACLE_HOME_SWITCH,
    CommandLineArgUtil.DOMAIN_HOME_SWITCH
]

__optional_arguments = [
    # Used by shell script to locate WLST
    CommandLineArgUtil.DOMAIN_TYPE_SWITCH,
    CommandLineArgUtil.ARCHIVE_FILE_SWITCH,
    CommandLineArgUtil.MODEL_FILE_SWITCH,
    CommandLineArgUtil.PREVIOUS_MODEL_FILE_SWITCH,
    CommandLineArgUtil.VARIABLE_FILE_SWITCH,
    CommandLineArgUtil.ADMIN_URL_SWITCH,
    CommandLineArgUtil.ADMIN_USER_SWITCH,
    CommandLineArgUtil.ADMIN_PASS_SWITCH,
    CommandLineArgUtil.USE_ENCRYPTION_SWITCH,
    CommandLineArgUtil.PASSPHRASE_SWITCH,
    CommandLineArgUtil.ROLLBACK_IF_RESTART_REQ_SWITCH
]


def __process_args(args):
    """
    Process the command-line arguments and prompt the user for any missing information
    :param args: the command-line arguments list
    :raises CLAException: if an error occurs while validating and processing the command-line arguments
    """
    global __wlst_mode

    cla_util = CommandLineArgUtil(_program_name, __required_arguments, __optional_arguments)
    cla_util.set_allow_multiple_models(True)
    required_arg_map, optional_arg_map = cla_util.process_args(args)

    cla_helper.verify_required_args_present(_program_name, __required_arguments, required_arg_map)
    cla_helper.validate_optional_archive(_program_name, optional_arg_map)
    cla_helper.validate_model_present(_program_name, optional_arg_map)
    cla_helper.validate_variable_file_exists(_program_name, optional_arg_map)

    __wlst_mode = cla_helper.process_online_args(optional_arg_map)
    cla_helper.process_encryption_args(optional_arg_map)

    combined_arg_map = optional_arg_map.copy()
    combined_arg_map.update(required_arg_map)
    return model_context_helper.create_context(_program_name, combined_arg_map)


def __deploy(model, model_context, aliases):
    """
    The method that does the heavy lifting for deploy.
    :param model: the model
    :param model_context: the model context
    :param aliases: the aliases
    :raises DeployException: if an error occurs
    """
    if __wlst_mode == WlstModes.ONLINE:
        ret_code = __deploy_online(model, model_context, aliases)
    else:
        ret_code = __deploy_offline(model, model_context, aliases)
    return ret_code


def __deploy_online(model, model_context, aliases):
    """
    Online deployment orchestration
    :param model: the model
    :param model_context: the model context
    :param aliases: the aliases object
    :raises: DeployException: if an error occurs
    """
    _method_name = '__deploy_online'

    admin_url = model_context.get_admin_url()
    admin_user = model_context.get_admin_user()
    admin_pwd = model_context.get_admin_password()

    __logger.info("WLSDPLY-09005", admin_url, method_name=_method_name, class_name=_class_name)

    try:
        __wlst_helper.connect(admin_user, admin_pwd, admin_url)
        deployer_utils.ensure_no_uncommitted_changes_or_edit_sessions()
        __wlst_helper.edit()
        __wlst_helper.start_edit()
    except BundleAwareException, ex:
        raise ex

    __logger.info("WLSDPLY-09007", admin_url, method_name=_method_name, class_name=_class_name)

    try:
        model_deployer.deploy_resources(model, model_context, aliases, wlst_mode=__wlst_mode)
    except DeployException, de:
        __release_edit_session_and_disconnect()
        raise de

    exit_code = 0

    try:
        # First we enable the stdout again and then redirect the stdoout to a string output stream
        # call isRestartRequired to get the output, capture the string and then silence wlst output again
        #
        __wlst_helper.enable_stdout()
        sostream = StringOutputStream()
        System.setOut(PrintStream(sostream))
        restart_required = __wlst_helper.is_restart_required()
        is_restartreq_output = sostream.get_string()
        __wlst_helper.silence()
        if model_context.is_rollback_if_restart_required() and restart_required:
            __wlst_helper.cancel_edit()
            __logger.severe('WLSDPLY_09015', is_restartreq_output)
            exit_code = CommandLineArgUtil.PROG_ROLLBACK_IF_RESTART_EXIT_CODE
        else:
            __wlst_helper.save()
            __wlst_helper.activate()
            if restart_required:
                exit_code = CommandLineArgUtil.PROG_RESTART_REQUIRED
    except BundleAwareException, ex:
        __release_edit_session_and_disconnect()
        raise ex

    model_deployer.deploy_applications(model, model_context, aliases, wlst_mode=__wlst_mode)

    try:
        __wlst_helper.disconnect()
    except BundleAwareException, ex:
        # All the changes are made and active so don't raise an error that causes the program
        # to indicate a failure...just log the error since the process is going to exit anyway.
        __logger.warning('WLSDPLY-09009', _program_name, ex.getLocalizedMessage(), error=ex,
                         class_name=_class_name, method_name=_method_name)
    return exit_code


def __deploy_offline(model, model_context, aliases):
    """
    Offline deployment orchestration
    :param model: the model
    :param model_context: the model context
    :param aliases: the aliases object
    :raises: DeployException: if an error occurs
    """
    _method_name = '__deploy_offline'

    domain_home = model_context.get_domain_home()
    __logger.info("WLSDPLY-09010", domain_home, method_name=_method_name, class_name=_class_name)

    __wlst_helper.read_domain(domain_home)

    model_deployer.deploy_model_offline(model, model_context, aliases, wlst_mode=__wlst_mode)

    try:
        __wlst_helper.update_domain()
    except BundleAwareException, ex:
        __close_domain_on_error()
        raise ex

    model_deployer.deploy_model_after_update(model, model_context, aliases, wlst_mode=__wlst_mode)

    try:
        __wlst_helper.close_domain()
    except BundleAwareException, ex:
        # All the changes are made so don't raise an error that causes the program to indicate
        # a failure...just log the error since the process is going to exit anyway.
        __logger.warning('WLSDPLY-09011', _program_name, ex.getLocalizedMessage(), error=ex,
                         class_name=_class_name, method_name=_method_name)
    return 0


def __release_edit_session_and_disconnect():
    """
    An online error recovery method.
    """
    _method_name = '__release_edit_session_and_disconnect'
    try:
        __wlst_helper.undo()
        __wlst_helper.stop_edit()
        __wlst_helper.disconnect()
    except BundleAwareException, ex:
        # This method is only used for cleanup after an error so don't mask
        # the original problem by throwing yet another exception...
        __logger.warning('WLSDPLY-09012', ex.getLocalizedMessage(), error=ex,
                         class_name=_class_name, method_name=_method_name)
    return


def __close_domain_on_error():
    """
    An offline error recovery method.
    """
    _method_name = '__close_domain_on_error'
    try:
        __wlst_helper.close_domain()
    except BundleAwareException, ex:
        # This method is only used for cleanup after an error so don't mask
        # the original problem by throwing yet another exception...
        __logger.warning('WLSDPLY-09013', ex.getLocalizedMessage(), error=ex,
                         class_name=_class_name, method_name=_method_name)
    return


def main(args):
    """
    The python entry point for deployApps.

    :param args:
    :return:
    """
    _method_name = 'main'

    __logger.entering(args[0], class_name=_class_name, method_name=_method_name)
    for index, arg in enumerate(args):
        __logger.finer('sys.argv[{0}] = {1}', str(index), str(arg), class_name=_class_name, method_name=_method_name)

    __wlst_helper.silence()

    exit_code = CommandLineArgUtil.PROG_OK_EXIT_CODE

    try:
        model_context = __process_args(args)
    except CLAException, ex:
        exit_code = ex.getExitCode()
        if exit_code != CommandLineArgUtil.HELP_EXIT_CODE:
            __logger.severe('WLSDPLY-20008', _program_name, ex.getLocalizedMessage(), error=ex,
                            class_name=_class_name, method_name=_method_name)
        cla_helper.clean_up_temp_files()

        # create a minimal model for summary logging
        model_context = model_context_helper.create_exit_context(_program_name)
        tool_exit.end(model_context, exit_code)

    aliases = Aliases(model_context, wlst_mode=__wlst_mode)

    model_dictionary = cla_helper.load_model(_program_name, model_context, aliases, "deploy", __wlst_mode)

    try:
        model = Model(model_dictionary)
        exit_code = __deploy(model, model_context, aliases)
    except DeployException, ex:
        __logger.severe('WLSDPLY-09015', _program_name, ex.getLocalizedMessage(), error=ex,
                        class_name=_class_name, method_name=_method_name)
        cla_helper.clean_up_temp_files()
        tool_exit.end(model_context, CommandLineArgUtil.PROG_ERROR_EXIT_CODE)

    cla_helper.clean_up_temp_files()

    tool_exit.end(model_context, exit_code)
    return


if __name__ == '__main__' or __name__ == 'main':
    WebLogicDeployToolingVersion.logVersionInfo(_program_name)
    main(sys.argv)

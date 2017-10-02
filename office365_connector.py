# --
# File: office365_connector.py
#
# Copyright (c) Phantom Cyber Corporation, 2017
#
# This unpublished material is proprietary to Phantom Cyber.
# All rights reserved. The methods and
# techniques described herein are considered trade secrets
# and/or confidential. Reproduction or distribution, in whole
# or in part, is forbidden except by express written permission
# of Phantom Cyber.
#
# --


# Phantom App imports
import phantom.app as phantom
from phantom.base_connector import BaseConnector
from phantom.action_result import ActionResult

# Usage of the consts file is recommended
from office365_consts import *
import requests
import json
from bs4 import BeautifulSoup
from django.http import HttpResponse
import time
import os
import pwd
import grp

TC_FILE = "oauth_task.out"
SERVER_TOKEN_URL = "https://login.microsoftonline.com/{0}/oauth2/v2.0/token"
MSGRAPH_API_URL = "https://graph.microsoft.com/v1.0"
MAX_END_OFFSET_VAL = 2147483646


class RetVal(tuple):
    def __new__(cls, val1, val2):
        return tuple.__new__(RetVal, (val1, val2))


def _process_empty_reponse(response, action_result):

    if response.status_code == 200:
        return RetVal(phantom.APP_SUCCESS, {})

    return RetVal(action_result.set_status(phantom.APP_ERROR, "Empty response and no information in the header"), None)


def _process_html_response(response, action_result):

    # An html response, treat it like an error
    status_code = response.status_code

    try:
        soup = BeautifulSoup(response.text, "html.parser")
        error_text = soup.text
        split_lines = error_text.split('\n')
        split_lines = [x.strip() for x in split_lines if x.strip()]
        error_text = '\n'.join(split_lines)
    except:
        error_text = "Cannot parse error details"

    message = "Status Code: {0}. Data from server:\n{1}\n".format(status_code,
            error_text)

    message = message.replace('{', '{{').replace('}', '}}')

    return RetVal(action_result.set_status(phantom.APP_ERROR, message), None)


def _process_json_response(r, action_result):

    # Try a json parse
    try:
        resp_json = r.json()
    except Exception as e:
        return RetVal(action_result.set_status(phantom.APP_ERROR, "Unable to parse JSON response. Error: {0}".format(str(e))), None)

    # Please specify the status codes here
    if 200 <= r.status_code < 399:
        return RetVal(phantom.APP_SUCCESS, resp_json)

    # You should process the error returned in the json
    message = "Error from server. Status Code: {0} Data from server: {1}".format(
            r.status_code, r.text.replace('{', '{{').replace('}', '}}'))

    return RetVal(action_result.set_status(phantom.APP_ERROR, message), None)


def _process_response(r, action_result):

    # store the r_text in debug data, it will get dumped in the logs if the action fails
    if hasattr(action_result, 'add_debug_data'):
        action_result.add_debug_data({'r_status_code': r.status_code})
        action_result.add_debug_data({'r_text': r.text})
        action_result.add_debug_data({'r_headers': r.headers})

    # Process each 'Content-Type' of response separately

    # Process a json response
    content_type = r.headers.get('Content-Type', '')
    if 'json' in content_type or 'javascript' in content_type:
        return _process_json_response(r, action_result)

    # Process an HTML resonse, Do this no matter what the api talks.
    # There is a high chance of a PROXY in between phantom and the rest of
    # world, in case of errors, PROXY's return HTML, this function parses
    # the error and adds it to the action_result.
    if 'html' in r.headers.get('Content-Type', ''):
        return _process_html_response(r, action_result)

    # it's not content-type that is to be parsed, handle an empty response
    if not r.text:
        return _process_empty_reponse(r, action_result)

    # everything else is actually an error at this point
    message = "Can't process response from server. Status Code: {0} Data from server: {1}".format(
            r.status_code, r.text.replace('{', '{{').replace('}', '}}'))

    return RetVal(action_result.set_status(phantom.APP_ERROR, message), None)


def _make_rest_call(action_result, url, verify=True, headers=None, params=None, data=None, method="get"):

    resp_json = None

    try:
        request_func = getattr(requests, method)
    except AttributeError:
        return RetVal(action_result.set_status(phantom.APP_ERROR, "Invalid method: {0}".format(method)), resp_json)

    try:
        r = request_func(
                        url,
                        data=data,
                        headers=headers,
                        verify=verify,
                        params=params)
    except Exception as e:
        return RetVal(action_result.set_status( phantom.APP_ERROR, "Error Connecting to server. Details: {0}".format(str(e))), resp_json)

    return _process_response(r, action_result)


def _load_app_state(asset_id, app_connector=None):
    """ Loads the data that was added to """

    # get the directory of the file
    dirpath = os.path.split(__file__)[0]
    state_file = "{0}/{1}_state.json".format(dirpath, asset_id)

    state = {}

    try:
        with open(state_file, 'r') as f:
            in_json = f.read()
            state = json.loads(in_json)
    except Exception as e:
        if (app_connector):
            app_connector.debug_print("In _load_app_state: Exception: {0}".format(str(e)))
        pass

    if (app_connector):
        app_connector.debug_print("Loaded state: ", state)

    return state


def _save_app_state(state, asset_id, app_connector):
    """ Saves the state into the same file """

    # get the directory of the file
    dirpath = os.path.split(__file__)[0]
    state_file = "{0}/{1}_state.json".format(dirpath, asset_id)

    if (app_connector):
        app_connector.debug_print("Saving state: ", state)

    try:
        with open(state_file, 'w+') as f:
            f.write(json.dumps(state))
    except Exception as e:
        print "Unable to save state file: {0}".format(str(e))
        pass

    return phantom.APP_SUCCESS


def _handle_oauth_result(request, path_parts):

    """
    <base_url>?admin_consent=True&tenant=a417c578-c7ee-480d-a225-d48057e74df5&state=13
    """
    asset_id = request.GET.get('state')
    if (not asset_id):
        return HttpResponse("ERROR: Asset ID not found in URL\n{0}".format(json.dumps(request.GET)))

    # first check for error info
    error = request.GET.get('error')
    error_description = request.GET.get('error_description')

    if (error):
        message = "Error: {0}".format(error)
        if (error_description):
            message += " Details: {0}".format(error_description)
        return HttpResponse("Server returned {0}".format(message))

    admin_consent = request.GET.get('admin_consent')
    if (not admin_consent):
        return HttpResponse("ERROR: admin_consent not found in URL\n{0}".format(json.dumps(request.GET)))

    # Load the data
    state = _load_app_state(asset_id)

    if (admin_consent == 'True'):
        admin_consent = True
    else:
        admin_consent = False

    state['admin_consent'] = admin_consent

    _save_app_state(state, asset_id, None)

    if (admin_consent):
        return HttpResponse("Admin Consent received. Please close this window, the action will continue to get new token")

    return HttpResponse("Admin Consent declined. Please close this window and try again later")


def _handle_oauth_start(request, path_parts):

    # get the asset id, the state file is created for each asset
    asset_id = request.GET.get('asset_id')
    if (not asset_id):
        return HttpResponse("ERROR: Asset ID not found in URL")

    # Load the state that was created for the asset
    state = _load_app_state(asset_id)

    # get the url to point to the authorize url of OAuth
    admin_consent_url = state.get('admin_consent_url')

    if (not admin_consent_url):
        return HttpResponse("App state is invalid, admin_consent_url key not found")

    # Redirect to this link, the user will then require to enter credentials interactively
    response = HttpResponse(status=302)
    response['Location'] = admin_consent_url

    return response


def handle_request(request, path_parts):
    """
    request contains the data posted to the rest endpoint, it is the django http request object
    path_parts is a list of the URL tokenized
    """

    # get the type of data requested, it's the last part of the URL used to post to the REST endpoint
    if (len(path_parts) < 2):
        return {'error': True, 'message': 'Invalid REST endpoint request'}

    call_type = path_parts[1]

    if (call_type == 'start_oauth'):
        # start the authentication process
        return _handle_oauth_start(request, path_parts)

    if (call_type == 'result'):

        # process the 'code'
        ret_val = _handle_oauth_result(request, path_parts)

        asset_id = request.GET.get('state')

        if (asset_id):
            # create the file that the 'test connectivity' action is waiting on
            app_dir = os.path.dirname(os.path.abspath(__file__))
            auth_status_file_path = "{0}/{1}_{2}".format(app_dir, asset_id, TC_FILE)
            open(auth_status_file_path, 'w').close()

            try:
                uid = pwd.getpwnam("apache").pw_uid
                gid = grp.getgrnam("phantom").gr_gid

                # set
                os.chown(auth_status_file_path, uid, gid)
                os.chmod(auth_status_file_path, "0664")
            except:
                pass

        return ret_val

    """
    if (call_type == 'refresh_token'):
        return _handle_oauth_refresh_token(request, path_parts)
    """

    return {'error': 'Invalid endpoint'}


def _get_dir_name_from_app_name(app_name):

    app_name = ''.join([x for x in app_name if x.isalnum()])
    app_name = app_name.lower()

    if (not app_name):
        # hardcode it
        app_name = "app_for_phantom"

    return app_name


class Office365Connector(BaseConnector):

    def __init__(self):

        # Call the BaseConnectors init first
        super(Office365Connector, self).__init__()

        self._state = None

        # Variable to hold a base_url in case the app makes REST calls
        # Do note that the app json defines the asset config, so please
        # modify this as you deem fit.
        self._base_url = None

    def _get_asset_name(self, action_result):

        asset_id = self.get_asset_id()

        rest_endpoint = PHANTOM_ASSET_INFO_URL.format(asset_id=asset_id)

        ret_val, resp_json = _make_rest_call(action_result, rest_endpoint, False)

        if (phantom.is_fail(ret_val)):
            return (ret_val, None)

        asset_name = resp_json.get('name')

        if (not asset_name):
            return (action_result.set_status(phantom.APP_ERROR, "Asset Name for id: {0} not found.".format(asset_id), None))

        return (phantom.APP_SUCCESS, asset_name)

    def _get_phantom_base_url(self, action_result):

        ret_val, resp_json = _make_rest_call(action_result, PHANTOM_SYS_INFO_URL, False)

        if (phantom.is_fail(ret_val)):
            return (ret_val, None)

        phantom_base_url = resp_json.get('base_url')

        if (not phantom_base_url):
            return (action_result.set_status(phantom.APP_ERROR,
                "Phantom Base URL not found in System Settings. Please specify this value in System Settings"), None)

        return (phantom.APP_SUCCESS, phantom_base_url)

    def _get_url_to_app_rest(self, action_result=None):

        if (not action_result):
            action_result = ActionResult()

        # get the phantom ip to redirect to
        ret_val, phantom_base_url = self._get_phantom_base_url(action_result)

        if (phantom.is_fail(ret_val)):
            return (action_result.get_status(), None)

        # get the asset name
        ret_val, asset_name = self._get_asset_name(action_result)

        if (phantom.is_fail(ret_val)):
            return (action_result.get_status(), None)

        self.save_progress('Using Phantom base URL as: {0}'.format(phantom_base_url))

        app_json = self.get_app_json()

        app_name = app_json['name']

        app_dir_name = _get_dir_name_from_app_name(app_name)

        url_to_app_rest = "{0}/rest/handler/{1}_{2}/{3}".format(phantom_base_url, app_dir_name, app_json['appid'], asset_name)

        return (phantom.APP_SUCCESS, url_to_app_rest)

    def _make_rest_call_helper(self, action_result, endpoint, verify=True, headers=None, params=None, data=None, method="get"):

        url = "{0}{1}".format(MSGRAPH_API_URL, endpoint)

        if (headers is None):
            headers = {}

        headers.update({
                'Authorization': 'Bearer {0}'.format(self._state['token']['access_token']),
                'Accept': 'application/json',
                'Content-Type': 'applicaiton/json'})

        return _make_rest_call(action_result, url, verify, headers, params, data, method)

    def _handle_test_connectivity(self, param):
        """ Function that handles the test connectivity action, it is much simpler than other action handlers."""

        # Progress
        # self.save_progress("Generating Authentication URL")
        app_state = {}
        action_result = self.add_action_result(ActionResult(param))

        self.save_progress("Getting App REST endpoint URL")
        # Get the URL to the app's REST Endpiont, this is the url that the TC dialog
        # box will ask the user to connect to
        ret_val, app_rest_url = self._get_url_to_app_rest(action_result)

        if (phantom.is_fail(ret_val)):
            self.save_progress("Unable to get the URL to the app's REST Endpoint. Error: {0}".format(
                action_result.get_message()))
            return self.set_status(phantom.APP_ERROR)

        # create the url that the oauth server should re-direct to after the auth is completed
        # (success and failure), this is added to the state so that the request handler will access
        # it later on
        redirect_uri = "{0}/result".format(app_rest_url)
        app_state['redirect_uri'] = redirect_uri

        self.save_progress("Using OAuth Redirect URL as:")
        self.save_progress(redirect_uri)

        config = self.get_config()

        # Create the url authorization, this is the one pointing to the oauth server side
        admin_consent_url = "https://login.microsoftonline.com/{0}/adminconsent".format(config['tenant'])
        admin_consent_url += "?client_id={0}".format(config['client_id'])
        admin_consent_url += "&redirect_uri={0}".format(redirect_uri)
        admin_consent_url += "&state={0}".format(self.get_asset_id())

        app_state['admin_consent_url'] = admin_consent_url

        # The URL that the user should open in a different tab.
        # This is pointing to a REST endpoint that points to the app
        url_to_show = "{0}/start_oauth?asset_id={1}&".format(app_rest_url, self.get_asset_id())

        # Save the state, will be used by the request handler
        _save_app_state(app_state, self.get_asset_id(), self)

        self.save_progress('Please connect to the following URL from a different tab to continue the connectivity process\n{0}'.format(url_to_show))

        time.sleep(5)

        completed = False

        app_dir = os.path.dirname(os.path.abspath(__file__))
        auth_status_file_path = "{0}/{1}_{2}".format(app_dir, self.get_asset_id(), TC_FILE)

        self.save_progress('Waiting for Admin Consent to complete')

        for i in range(0, 40):

            self.send_progress('{0}'.format('.' * (i % 10)))

            if (os.path.isfile(auth_status_file_path)):
                completed = True
                os.unlink(auth_status_file_path)
                break

            time.sleep(TC_STATUS_SLEEP)

        if (not completed):
            self.save_progress("Authentication process does not seem to be completed. Timing out")
            return self.set_status(phantom.APP_ERROR)

        self.send_progress("")

        # Load the state again, since the http request handlers would have saved the result of the admin consent
        app_state = _load_app_state(self.get_asset_id(), self)

        if (not app_state) or (not app_state.get('admin_consent')):
            self.save_progress("Admin consent not received or not given")
            self.save_progress("Test Connectivity Failed")
            return self.set_status(phantom.APP_ERROR)

        # The authentication seems to be done, let's see if it was successfull
        self._state['admin_consent'] = app_state['admin_consent']

        self.save_progress("Getting the token")
        ret_val = self._get_token(action_result)
        if (phantom.is_fail(ret_val)):
            return action_result.get_status()

        self.save_progress("Getting info about a single user to verify token")
        params = {'$top': '1'}
        ret_val, response = self._make_rest_call_helper(action_result, "/users", params=params)
        if (phantom.is_fail(ret_val)):
            self.save_progress("API to get users failed: {0}".format(action_result.get_message()))
            self.save_progress("Test Connectivity Failed")
            return self.set_status(phantom.APP_ERROR)

        value = response.get('value')

        if (value):
            self.save_progress("Got user info")

        self.save_progress("Test Connectivity Passed")

        return self.set_status(phantom.APP_SUCCESS)

    def _handle_copy_email(self, param):

        # Implement the handler here
        # use self.save_progress(...) to send progress messages back to the platform
        self.save_progress("In action handler for: {0}".format(self.get_action_identifier()))

        # Add an action result object to self (BaseConnector) to represent the action for this param
        action_result = self.add_action_result(ActionResult(dict(param)))

        """
        # Access action parameters passed in the 'param' dictionary

        # Required values can be accessed directly
        required_parameter = param['required_parameter']

        # Optional values should use the .get() function
        optional_parameter = param.get('optional_parameter', 'default_value')
        """

        """
        # make rest call
        ret_val, response = self._make_rest_call('/endpoint', action_result, params=None, headers=None)

        if (phantom.is_fail(ret_val)):
            # the call to the 3rd party device or service failed, action result should contain all the error details
            # so just return from here
            return action_result.get_status()

        # Now post process the data,  uncomment code as you deem fit

        # Add the response into the data section
        # action_result.add_data(response)
        """

        action_result.add_data({})

        # Add a dictionary that is made up of the most important values from data into the summary
        summary = action_result.update_summary({})
        summary['important_data'] = "value"

        # Return success, no need to set the message, only the status
        # BaseConnector will create a textual message based off of the summary dictionary
        # return action_result.set_status(phantom.APP_SUCCESS)

        # For now return Error with a message, in case of success we don't set the message, but use the summary
        return action_result.set_status(phantom.APP_ERROR, "Action not yet implemented")

    def _handle_delete_email(self, param):

        # Implement the handler here
        # use self.save_progress(...) to send progress messages back to the platform
        self.save_progress("In action handler for: {0}".format(self.get_action_identifier()))

        # Add an action result object to self (BaseConnector) to represent the action for this param
        action_result = self.add_action_result(ActionResult(dict(param)))

        """
        # Access action parameters passed in the 'param' dictionary

        # Required values can be accessed directly
        required_parameter = param['required_parameter']

        # Optional values should use the .get() function
        optional_parameter = param.get('optional_parameter', 'default_value')
        """

        """
        # make rest call
        ret_val, response = self._make_rest_call('/endpoint', action_result, params=None, headers=None)

        if (phantom.is_fail(ret_val)):
            # the call to the 3rd party device or service failed, action result should contain all the error details
            # so just return from here
            return action_result.get_status()

        # Now post process the data,  uncomment code as you deem fit

        # Add the response into the data section
        # action_result.add_data(response)
        """

        action_result.add_data({})

        # Add a dictionary that is made up of the most important values from data into the summary
        summary = action_result.update_summary({})
        summary['important_data'] = "value"

        # Return success, no need to set the message, only the status
        # BaseConnector will create a textual message based off of the summary dictionary
        # return action_result.set_status(phantom.APP_SUCCESS)

        # For now return Error with a message, in case of success we don't set the message, but use the summary
        return action_result.set_status(phantom.APP_ERROR, "Action not yet implemented")

    def _handle_get_email(self, param):

        # Implement the handler here
        # use self.save_progress(...) to send progress messages back to the platform
        self.save_progress("In action handler for: {0}".format(self.get_action_identifier()))

        # Add an action result object to self (BaseConnector) to represent the action for this param
        action_result = self.add_action_result(ActionResult(dict(param)))

        """
        # Access action parameters passed in the 'param' dictionary

        # Required values can be accessed directly
        required_parameter = param['required_parameter']

        # Optional values should use the .get() function
        optional_parameter = param.get('optional_parameter', 'default_value')
        """

        """
        # make rest call
        ret_val, response = self._make_rest_call('/endpoint', action_result, params=None, headers=None)

        if (phantom.is_fail(ret_val)):
            # the call to the 3rd party device or service failed, action result should contain all the error details
            # so just return from here
            return action_result.get_status()

        # Now post process the data,  uncomment code as you deem fit

        # Add the response into the data section
        # action_result.add_data(response)
        """

        action_result.add_data({})

        # Add a dictionary that is made up of the most important values from data into the summary
        summary = action_result.update_summary({})
        summary['important_data'] = "value"

        # Return success, no need to set the message, only the status
        # BaseConnector will create a textual message based off of the summary dictionary
        # return action_result.set_status(phantom.APP_SUCCESS)

        # For now return Error with a message, in case of success we don't set the message, but use the summary
        return action_result.set_status(phantom.APP_ERROR, "Action not yet implemented")

    def _handle_on_poll(self, param):

        # Implement the handler here
        # use self.save_progress(...) to send progress messages back to the platform
        self.save_progress("In action handler for: {0}".format(self.get_action_identifier()))

        # Add an action result object to self (BaseConnector) to represent the action for this param
        action_result = self.add_action_result(ActionResult(dict(param)))

        """
        # Access action parameters passed in the 'param' dictionary

        # Required values can be accessed directly
        required_parameter = param['required_parameter']

        # Optional values should use the .get() function
        optional_parameter = param.get('optional_parameter', 'default_value')
        """

        """
        # make rest call
        ret_val, response = self._make_rest_call('/endpoint', action_result, params=None, headers=None)

        if (phantom.is_fail(ret_val)):
            # the call to the 3rd party device or service failed, action result should contain all the error details
            # so just return from here
            return action_result.get_status()

        # Now post process the data,  uncomment code as you deem fit

        # Add the response into the data section
        # action_result.add_data(response)
        """

        action_result.add_data({})

        # Add a dictionary that is made up of the most important values from data into the summary
        summary = action_result.update_summary({})
        summary['important_data'] = "value"

        # Return success, no need to set the message, only the status
        # BaseConnector will create a textual message based off of the summary dictionary
        # return action_result.set_status(phantom.APP_SUCCESS)

        # For now return Error with a message, in case of success we don't set the message, but use the summary
        return action_result.set_status(phantom.APP_ERROR, "Action not yet implemented")

    def _validate_range(self, email_range, action_result):

        try:
            mini, maxi = (int(x) for x in email_range.split('-'))
        except:
            return action_result.set_status(phantom.APP_ERROR, "Unable to parse the range. Please specify the range as min_offset-max_offset")

        if (mini < 0) or (maxi < 0):
            return action_result.set_status(phantom.APP_ERROR, "Invalid min or max offset value specified in range", )

        if (mini > maxi):
            return action_result.set_status(phantom.APP_ERROR, "Invalid range value, min_offset greater than max_offset")

        if (maxi > MAX_END_OFFSET_VAL):
            return action_result.set_status(phantom.APP_ERROR, "Invalid range value. The max_offset value cannot be greater than {0}".format(MAX_END_OFFSET_VAL))

        return (phantom.APP_SUCCESS)

    def _handle_generate_token(self, param):

        action_result = self.add_action_result(ActionResult(dict(param)))
        ret_val = self._get_token(action_result)
        if (phantom.is_fail(ret_val)):
            return action_result.get_status()

        self._state['admin_consent'] = True

        return action_result.set_status(phantom.APP_SUCCESS, "Token generated")

    def _handle_run_query(self, param):

        # Implement the handler here
        # use self.save_progress(...) to send progress messages back to the platform
        self.save_progress("In action handler for: {0}".format(self.get_action_identifier()))

        # Add an action result object to self (BaseConnector) to represent the action for this param
        action_result = self.add_action_result(ActionResult(dict(param)))

        # user
        endpoint = "/users/{0}".format(param['email'])

        # folder
        if ('folder' in param):
            endpoint += '/mailFolders/{0}'.format(param['folder'])

        # that should be enough to create the endpoint
        endpoint += '/messages'

        # range
        email_range = param.get('range', "0-10")
        ret_val = self._validate_range(email_range, action_result)

        if (phantom.is_fail(ret_val)):
            return action_result.get_status()

        mini, maxi = (int(x) for x in email_range.split('-'))
        params = {'$top': str(maxi - mini + 1), '$skip': str(mini)}

        query_filter = ''
        if ('subject' in param):
            if (query_filter):
                query_filter += ' and '
            query_filter += " contains(subject, '{0}')".format(param['subject'])

        if ('sender' in param):
            if (query_filter):
                query_filter += ' and '
            query_filter += " (sender/emailAddress/address) eq '{0}'".format(param['sender'])

        if ('internet_message_id' in param):
            if (query_filter):
                query_filter += ' and '
            query_filter += " (internetMessageId) eq '{0}'".format(param['internet_message_id'])

        if (query_filter):
            params['$filter'] = query_filter

        query_string = param.get('query')

        if (query_string):

            # ignore everything else
            params = None
            endpoint += "?{0}".format(query_string)

        ret_val, response = self._make_rest_call_helper(action_result, endpoint, params=params)
        if (phantom.is_fail(ret_val)):
            return action_result.get_status()

        value = response.get('value')

        for curr_value in value:
            action_result.add_data(curr_value)

        action_result.update_summary({'emails_matched': action_result.get_data_size()})

        return action_result.set_status(phantom.APP_SUCCESS)

    def handle_action(self, param):

        ret_val = phantom.APP_SUCCESS

        # Get the action that we are supposed to execute for this App Run
        action_id = self.get_action_identifier()

        self.debug_print("action_id", self.get_action_identifier())

        if action_id == 'test_connectivity':
            ret_val = self._handle_test_connectivity(param)

        elif action_id == 'copy_email':
            ret_val = self._handle_copy_email(param)

        elif action_id == 'delete_email':
            ret_val = self._handle_delete_email(param)

        elif action_id == 'get_email':
            ret_val = self._handle_get_email(param)

        elif action_id == 'on_poll':
            ret_val = self._handle_on_poll(param)

        elif action_id == 'run_query':
            ret_val = self._handle_run_query(param)

        elif action_id == 'generate_token':
            ret_val = self._handle_generate_token(param)

        return ret_val

    def _get_token(self, action_result):

        config = self.get_config()

        client_id = config['client_id']

        client_secret = config['client_secret']

        tenant = config['tenant']

        req_url = SERVER_TOKEN_URL.format(tenant)
        data = {
                'client_id': client_id,
                'scope': 'https://graph.microsoft.com/.default',
                'client_secret': client_secret,
                'grant_type': 'client_credentials'}

        ret_val, resp_json = _make_rest_call(action_result, req_url, data=data)

        if (phantom.is_fail(ret_val)):
            return action_result.get_status()

        self._state['token'] = resp_json

        return (phantom.APP_SUCCESS)

    def initialize(self):

        # Load the state in initialize
        self._state = self.load_state()

        # check if admin consent was granted
        admin_consent = self._state.get('admin_consent')
        action_id = self.get_action_identifier()

        if (action_id == 'generate_token'):
            return phantom.APP_SUCCESS

        # if it was not and the current action is not test connectivity then it's an error
        if (not admin_consent) and (action_id != 'test_connectivity'):
            return self.set_status(phantom.APP_ERROR,
                    "Please Run test connectivity first to get an admin consent and generate a token that the app can use to make calls to the server")

        if (action_id == 'test_connectivity'):
            # User is trying to get the admin consent, so just return True from here so that test connectivity continues
            return phantom.APP_SUCCESS

        # if reached here, means it is some other action and admin has consented, so let's get a token
        action_result = ActionResult()
        ret_val = self._get_token(action_result)
        if (phantom.is_fail(ret_val)):
            return self.set_status(phantom.APP_ERROR, action_result.get_message())

        return phantom.APP_SUCCESS

    def finalize(self):

        # Save the state, this data is saved accross actions and app upgrades
        self.save_state(self._state)
        return phantom.APP_SUCCESS


if __name__ == '__main__':

    import sys
    import pudb
    pudb.set_trace()

    if (len(sys.argv) < 2):
        print "No test json specified as input"
        exit(0)

    with open(sys.argv[1]) as f:
        in_json = f.read()
        in_json = json.loads(in_json)
        print(json.dumps(in_json, indent=4))

        connector = Office365Connector()
        connector.print_progress_message = True
        ret_val = connector._handle_action(json.dumps(in_json), None)
        print (json.dumps(json.loads(ret_val), indent=4))

    exit(0)
import json
import os
import traceback
import boto3
import decimal
import requests
from boto3.dynamodb.conditions import Key, Attr
from botocore.exceptions import ClientError


dynamodb = boto3.resource('dynamodb')
projects_table = os.environ['PROJECTS_TABLE']


#=========================================#
#=======     Helper Functions     ========#
#=========================================#

def create_response(statusCode, message):
    """Returns a given status code."""

    return { 
        'statusCode': statusCode,
        'headers': {
            # Required for CORS support to work
            'Access-Control-Allow-Origin': '*',
            # Required for cookies, authorization headers with HTTPS
            'Access-Control-Allow-Credentials': 'true',
        },
        'body': message
    }


class DecimalEncoder(json.JSONEncoder):
    """Helper class to convert a DynamoDB item to JSON."""

    def default(self, o):
        if isinstance(o, set):
            return list(o)
        if isinstance(o, decimal.Decimal):
            if o % 1 != 0:
                return float(o)
            else:
                return int(o)
        return super(DecimalEncoder, self).default(o)


def get_calendar_url(path):
    """Where the calendar this projects backend belongs to actually lives.

    PTR_CALENDAR_ROOT first, which is how every other component in this stack
    is told where the services are. Without it the only possible answer was
    calendar.photonranch.org, so a deployment with its own calendar -- the
    Proxmox lab, or anything run offline -- aimed its writes at LCO production.
    This function is only used to DELETE things from a calendar, which makes
    that the wrong default to fail to.
    """
    root = os.getenv('PTR_CALENDAR_ROOT')
    if root:
        return f"{root.rstrip('/')}/{path}"

    # Otherwise the original behaviour. The development stage is used in some
    # URLs. The production URL for the calendar is '...org/calendar...', so
    # check first if the stage is 'prod'.
    stage = os.environ['STAGE']
    if stage == 'prod':
        stage = 'calendar'
    return f"https://calendar.photonranch.org/{stage}/{path}"


def removeProjectFromCalendarEvents(list_of_event_ids):
    """Removes a project from associated reservations in the calendar.

    Args:
        list_of_event_ids (list): Ids of calendar events we want to modify.
    """

    # Nothing to do, and worth returning early rather than posting an empty
    # list: until the calendar started registering bookings on their projects,
    # scheduled_with_events was empty on every project and this was always the
    # case, which is why deleting a project never cleared its bookings.
    if not list_of_event_ids:
        print("project has no associated calendar events to clear")
        return

    requestBody = json.dumps({
        "events": list_of_event_ids
    })
    requests.post(get_calendar_url('remove-project-from-events'), requestBody,
                  timeout=10)


#=========================================#
#=======       Core Methods       ========#
#=========================================#
    
def modify_project(project_name: str, created_at: str, project_changes: dict):
    """Modifies the details of an exising project.

    Args:
        project_name (str): Name of the existing project we want to modify.
        created_at (str): UTC ISO datetime of project creation, used to id 
            the existing project we want to modify.
        project_changes (dict): These are the changes we want to apply. The 
            format of this dict should be the same as if we were adding a new
            project.

    Returns:
        dict: contains the following keys:
            is_successful (bool): whether or not the update worked.
            description (str): optional text to display to the user.
            updated_project (str): the state of the project after the update.
    """

    table = dynamodb.Table(projects_table)

    old_project = get_project(project_name, created_at)

    # If the project specified by project_name and created_at is not found:
    if not old_project["project_exists"]: 
        return {
            "is_successful": False,
            "description": "The requested project does not exist.",
            "updated_project": []
        }

    # Initialize the dict that will overwrite the existing project in dynamodb.
    updated_project = old_project["project"]

    # Apply the new project changes we want to make
    updated_project["project_constraints"] = project_changes["project_constraints"]
    updated_project["project_name"] = project_changes["project_name"]
    updated_project["project_note"] = project_changes["project_note"]
    updated_project["project_targets"] = project_changes["project_targets"]
    updated_project["project_sites"] = project_changes["project_sites"]
    updated_project["scheduled_with_events"] = project_changes["scheduled_with_events"]
    updated_project["project_priority"] = project_changes["project_priority"]

    # A tricky detail is how to keep track of existing project data for 
    # exposure requests that have been modified. 

    # Note: this treats identical exposure requests with different image counts 
    # as different requests. In other words, editing a project by increasing the
    # number of images for some exposure will start from scratch, ignoring 
    # any previously gathered data. 

    # Initialize a new array to store identifiers for completed project data
    updated_project_data = [[] for x in range(len(project_changes["exposures"]))]
    updated_remaining_data = [exposure["count"] for exposure in project_changes["exposures"]]

    # For each exposure request, try to match it with an existing exposure 
    # request. If they match, then 'import' the associated data into the 
    # updated_project_data array. 
    # project_data and remaining are parallel to exposures in intent but not in
    # guarantee: a project can be stored with fewer entries than it has
    # exposures -- 16 of the 23 in this table were, with project_data [] against
    # one exposure, all from the same creation script -- and indexing them by
    # old_index then raised IndexError and took the whole edit down. Editing any
    # of those projects was impossible, and the failure surfaced as an opaque
    # 500 because the handler's except clause could not serialise the error.
    #
    # A missing entry means nothing has been gathered for that exposure yet,
    # which is exactly what updated_project_data and updated_remaining_data are
    # already initialised to. So skip the import and keep the default rather
    # than inventing one.
    old_project_data = old_project["project"].get("project_data")
    old_remaining = old_project["project"].get("remaining")
    # Compare both sides in the same types. Without this the match below failed
    # for any exposure carrying a float that is not exactly representable --
    # width and height routinely are not -- so prior progress was never
    # imported and every edit quietly reset a project's completion counts: a
    # project nine frames into ten went back to ten remaining, with its
    # project_data cleared. The loop has always meant to carry that forward.
    incoming_exposures = _as_json_types(project_changes["exposures"])
    stored_exposures = _as_json_types(old_project["project"].get("exposures") or [])
    for new_index, new_exposure in enumerate(incoming_exposures):
        for old_index, old_exposure in enumerate(stored_exposures):
            if new_exposure == old_exposure:
                if isinstance(old_project_data, list) and old_index < len(old_project_data):
                    updated_project_data[new_index] = old_project_data[old_index]
                if isinstance(old_remaining, list) and old_index < len(old_remaining):
                    updated_remaining_data[new_index] = old_remaining[old_index]
                break                   

    # Finally, add the updated_project_data array to the udpated_project dict.
    updated_project["project_data"] = updated_project_data
    updated_project["remaining"] = updated_remaining_data
    updated_project["exposures"] = project_changes["exposures"]

    # Delete the existing project from the table
    table.delete_item(
        Key={
            "project_name": project_name,
            "created_at": created_at
        },
    )
    # Add the updated project back
    dynamodb_entry = json.loads(json.dumps(updated_project, cls=DecimalEncoder), parse_float=decimal.Decimal)
    table_response = table.put_item(Item=dynamodb_entry)
    
    return {
        "is_successful": True,
        "description": "Project has been updated.",
        "updated_project": table_response,
    }
        
    
def _as_json_types(value):
    """Re-read a stored value as JSON would have delivered it.

    DynamoDB hands back Decimal; a request body arrives as int and float. The
    two do not compare equal when the number has no exact binary form --
    Decimal('2.398') == 2.398 is False, because Decimal compares exactly -- so
    an exposure read from the table never matched the same exposure sent over
    HTTP. Putting the stored side through the encoder the response path already
    uses leaves both in the same types.
    """
    return json.loads(json.dumps(value, cls=DecimalEncoder))


def get_project(project_name, created_at):
    """Retrieves details of a specified project from the DynamoDB table.
    
    Args:
        project_name (str): Name of the project we want to retrieve.
        created_at (str): UTC datetime string of project creation.

    Returns:
        List of project details, if it exists.
        Otherwise, an empty list.
    """

    table = dynamodb.Table(projects_table)

    response = table.get_item(
        Key={
            "project_name": project_name,
            "created_at": created_at,
        }
    )
    if 'Item' in response:
        return {
            "project_exists": True,
            "project": response['Item']
        }
    else: 
        return {
            "project_exists": False,
            "project": []
        }


#=========================================#
#=======          Handlers        ========#
#=========================================#

def addNewProject(event, context):
    """Adds a new project to the projects DynamoDB database.

    Args:
        event.body.project_name (str): Name of the project we want to add.
        event.body.user_id (str): Auth0 user 'sub'.
        event.body.created_at (str): UTC datetime string of project creation.

    Returns:
        200 status code with project details if successful.
        400 status code if project missing required keys.
    """
    
    event_body = json.loads(event.get("body", ""))
    table = dynamodb.Table(projects_table)

    print("event_body:")
    print(event_body)

    # Check that all required keys are present.
    required_keys = ['project_name', 'user_id', 'created_at']
    actual_keys = event_body.keys()
    for key in required_keys:
        if key not in actual_keys:
            msg = f"Error: missing required key {key}"
            print(msg)
            return create_response(400, msg)

    # Those three identify a project; they do not describe one. Nothing else
    # used to be checked, so a body carrying only them was stored verbatim and
    # answered 200 -- and the interface then broke on what came back, because
    # the event editor reads project_sites off every record in the list without
    # guarding. Two such records were created by hand on 2026-10-07 and between
    # them they blanked the calendar's editor for every drop, on every site,
    # until the records were removed.
    #
    # There used to be a sharper version of the same mistake: put_item replaced
    # the stored item rather than merging into it, so re-posting an existing
    # project with a partial body silently discarded every field the second body
    # left out. One of those two records lost a 29-field project_constraints
    # that way, 47 milliseconds after gaining it. The conditional put below
    # closes that; this check is about the first write rather than the second.
    describing_keys = ['project_sites', 'project_targets', 'exposures',
                       'project_constraints']
    missing = [k for k in describing_keys if k not in actual_keys]
    if missing:
        msg = (f"Error: missing required key(s) {', '.join(missing)}. "
               "A project must be posted whole: the interface and the rest of "
               "this service read these fields without guarding.")
        print(msg)
        return create_response(400, msg)

    # The owner is whoever is calling, not whoever the body says. user_id used
    # to be taken straight from the request body -- supplied by the browser and
    # settable to anything -- so a project could be created in another person's
    # name and would then appear in their list and not the author's. API Gateway
    # puts the verified caller in requestContext; the local runner mirrors the
    # same shape with its dev principal, so an unauthenticated create is now
    # refused here too rather than silently attributed.
    #
    # Refusing rather than overwriting is deliberate: a caller that disagrees
    # with the gateway about who it is has a bug worth seeing, and a script
    # driving this endpoint can still create projects by naming the principal
    # it actually has.
    caller = (event.get("requestContext", {})
                   .get("authorizer", {})
                   .get("principalId"))
    if caller:
        claimed = event_body["user_id"]
        if claimed != caller:
            msg = (f"Error: a project may only be created for the caller. This "
                   f"request is authenticated as {caller} but names {claimed} "
                   "as the owner.")
            print(msg)
            return create_response(403, msg)

    # The one field the rest of this service subscripts without checking:
    # addProjectEvent and deleteProject both do `Item['scheduled_with_events']`.
    # A project stored without it could afterwards be neither booked nor
    # deleted -- the delete path raised KeyError before it reached the table,
    # so the malformed records could only be removed from DynamoDB directly.
    event_body.setdefault('scheduled_with_events', [])

    # Convert floats into decimals for dynamodb
    dynamodb_entry = json.loads(json.dumps(event_body), parse_float=decimal.Decimal)

    # Insert-only. An unconditional put_item REPLACES whatever is stored at this
    # key, so posting an existing project here used to discard it and answer 200
    # -- a caller could not tell a creation from an overwrite, and neither could
    # the log. A project is identified by project_name + created_at, the
    # interface stamps created_at at send time, and in the whole service log no
    # key has ever been posted twice except by a bisect run against this
    # endpoint. Changing an existing project is what /modify-project is for.
    try:
        table_response = table.put_item(
            Item=dynamodb_entry,
            ConditionExpression=(
                "attribute_not_exists(project_name) AND "
                "attribute_not_exists(created_at)"
            ),
        )
    except ClientError as e:
        if e.response['Error']['Code'] == "ConditionalCheckFailedException":
            msg = (f"Error: a project named {dynamodb_entry['project_name']} "
                   f"created at {dynamodb_entry['created_at']} already exists. "
                   "Use /modify-project to change it -- /new-project will not "
                   "overwrite.")
            print(msg)
            return create_response(409, msg)
        print(f"error adding project: {e}")
        return create_response(400, e.response['Error']['Message'])

    message = json.dumps({
        'table_response': table_response,
        'new_project': event_body,
    })
    return create_response(200, message)


def modify_project_handler(event, context):
    """Handler method to create a response code after modifying a project.

    Args:
       event.body.project_name (str): Name of the project we want to modify.
       event.body.created_at (str): UTC datetime string of project creation.
       event.body.project_changes (dict): Project changes to apply.

    Returns:
        200 status code with modified project details if successful.
        400 status code if unsuccessful.
    """
    
    try:
        event_body = json.loads(event.get("body", ""))
        print(event_body)

        project_name = event_body['project_name']
        created_at = event_body['created_at']
        project_changes = event_body['project_changes']

        # Until 2026-10-09 this endpoint had no authorizer and no ownership
        # check of any kind, so anyone who could reach it could rewrite anyone
        # else's project -- including its targets, exposures and sites, with
        # the owner never told. deleteProject has guarded itself this way all
        # along; modify-project, which can change just as much, did not.
        #
        # modify_project deletes the existing row and puts the amended one
        # back, so an unauthorised call does not merely edit, it replaces.
        # Check before touching anything.
        authorizer = event.get("requestContext", {}).get("authorizer", {})
        caller = authorizer.get("principalId")
        if caller:
            try:
                roles = json.loads(authorizer.get("userRoles") or "[]")
            except (TypeError, ValueError):
                roles = []
            existing = get_project(project_name, created_at)
            if not existing["project_exists"]:
                return create_response(404, "No project found to modify.")
            owner = existing["project"].get("user_id")
            if "admin" not in roles and owner != caller:
                msg = ("Error: you may only modify your own projects. This "
                       f"project belongs to {owner}.")
                print(msg)
                return create_response(403, msg)

        response = modify_project(project_name, created_at, project_changes)
        return create_response(200, json.dumps(response, cls=DecimalEncoder))
    
    # Something else went wrong, return a Bad Request status code.
    except Exception as e:
        # json.dumps(e) raises TypeError -- an exception is not serializable --
        # so this except clause used to raise from inside itself and the caller
        # got an opaque 500 with the real error nowhere in the response. It hid
        # a live IndexError in modify_project for as long as it has existed.
        print(f"Exception: {e}")
        print(traceback.format_exc())
        return create_response(400, f"{type(e).__name__}: {e}")


def get_project_handler(event, context):
    """Handler method to retrieve the details of a project.

    Args:
        event.body.project_name (str): Name of the existing project to modify.
        event.body.created_at (str): UTC datetime string of project creation.

    Returns:
        200 status code with project details if successful.
        Otherwise, 404 status code if project does not exist.
    """
    
    event_body = json.loads(event.get("body", ""))

    print("event_body:")
    print(event_body)

    project_name = event_body['project_name']
    created_at = event_body['created_at']

    project = get_project(project_name, created_at)
    if project["project_exists"]:
        project_json = json.dumps(project["project"], cls=DecimalEncoder)
        return create_response(200, project_json)
    else: 
        return create_response(404, "Project not found.")


def getAllProjects(event, context):
    """Retrieves all existing projects and details from the DynamoDB table.
    
    Returns:
        200 status code with JSON of all project data.

    Example Python code using this endpoint:
        import requests
        url = "https://projects.photonranch.org/dev/get-all-projects"
        all_projects = requests.post(url).json()
    """

    table = dynamodb.Table(projects_table)

    response = table.scan()
    data = response['Items']

    while 'LastEvaluatedKey' in response:
        response = table.scan(ExclusiveStartKey=response['LastEvaluatedKey'])
        data.extend(response['Items'])

    return create_response(200, json.dumps(data, cls=DecimalEncoder))


def getUserProjects(event, context):
    """Retrieves the details of all projects created by a specified user.

    Args:
        event.body.user_id (str): Auth0 user 'sub'.

    Returns:
        200 status code with JSON of user project details.
        400 status code if the required key 'user_id' is missing.
    """
    
    event_body = json.loads(event.get("body", ""))
    table = dynamodb.Table(projects_table)

    print("event_body:")
    print(event_body)

    # Check that all required keys are present.
    required_keys = ['user_id']
    actual_keys = event_body.keys()
    for key in required_keys:
        if key not in actual_keys:
            msg = f"Error: missing required key {key}"
            print(msg)
            return create_response(400, json.dumps(msg))

    response = table.query(
        IndexName="userid-createdat-index",
        KeyConditionExpression=Key('user_id').eq(event_body['user_id'])
    )
    print(response)
    user_projects = json.dumps(response['Items'], cls=DecimalEncoder)

    return create_response(200, user_projects)


def addProjectEvent(event, context):
    """Adds an associated calendar event to a project's list of events.

    Projects keep a list of events that they are scheduled with. 
    This way, if a project is deleted, it can be removed from any 
    associated events.

    We could use a set in DynamoDB to keep track of associated events. But that
    gets complicated because JSON does not support sets, so we would need lots
    of custom modifier code throughout the pipeline. 

    Instead we'll keep it simple with a list. Get the list, check if already 
    contains our event, and add it if not, then update DynamoDB. 

    Args:
        event.body.project_name (str): Name of the project to add events to.
        event.body.created_at (str): UTC datetime string of project creation.
        event.body.event_id (str): id of the associated calendar event to add.

    Returns:
        200 status code if calendar event already exists in project's details.
        200 status code if successful adding event ids to the project.
    """

    request_body = json.loads(event.get("body", ""))
    table = dynamodb.Table(projects_table)

    print("event_body:")
    print(request_body)

    project_name = request_body["project_name"]
    created_at = request_body["created_at"]
    event_id = request_body["event_id"]  # ID of the calendar event

    response = table.get_item(
        Key={
            "project_name": project_name,
            "created_at": created_at
        },
    )
    events_list = response['Item']['scheduled_with_events']

    # Don't add a duplicate
    if event_id in events_list:
        return create_response(200, 'Event already associated with this project')

    # Add the event to the list and then update the project in dynamodb
    else:
        events_list.append(event_id)
        print(f"events_list: {events_list}")

        update_response = table.update_item(
            Key={
                "project_name": project_name,
                "created_at": created_at,
            },
            UpdateExpression="SET scheduled_with_events = :swe",
            ExpressionAttributeValues={
                ":swe": events_list
            }
        )
        return create_response(200, 'Successfully associated event with project.')


def removeProjectEvent(event, context):
    """Removes a calendar event from a project's list of events.

    The mirror of addProjectEvent, and the other half of keeping
    scheduled_with_events true. Without it the list only ever grew: a booking
    deleted from the calendar stayed listed on its project forever, so the
    field could not be read as "the bookings that will run this project" --
    only as "bookings that were made at some point".

    Harmless in its first consumer -- deleteProject asks the calendar to clear
    a project from an event that no longer exists, which does nothing -- but a
    list that accumulates ids nobody can resolve is the kind of thing a later
    feature trusts by mistake.

    Args:
        event.body.project_name (str): Name of the project to remove from.
        event.body.created_at (str): UTC datetime string of project creation.
        event.body.event_id (str): id of the calendar event to remove.

    Returns:
        200 whether or not the event was listed, and whether or not the
        project still exists. This is called while deleting a booking, and a
        booking must not fail to delete because its project is already gone.
    """

    request_body = json.loads(event.get("body", ""))
    table = dynamodb.Table(projects_table)

    project_name = request_body["project_name"]
    created_at = request_body["created_at"]
    event_id = request_body["event_id"]

    response = table.get_item(
        Key={
            "project_name": project_name,
            "created_at": created_at
        },
    )

    # The project may have been deleted before the booking was. Nothing to
    # update, and nothing wrong.
    if "Item" not in response:
        return create_response(200, 'No such project; nothing to remove.')

    events_list = response['Item'].get('scheduled_with_events') or []
    if event_id not in events_list:
        return create_response(200, 'Event was not associated with this project')

    events_list = [e for e in events_list if e != event_id]
    table.update_item(
        Key={
            "project_name": project_name,
            "created_at": created_at,
        },
        UpdateExpression="SET scheduled_with_events = :swe",
        ExpressionAttributeValues={
            ":swe": events_list
        }
    )
    return create_response(200, 'Successfully removed event from project.')


def addProjectData(event, context):
    """Updates a project with images taken to track the completion progress.

    When an observatory captures and uploads an image requested in a project,
    it should use this endpoint to update the project's completion status.

    Args:
        event.body.project_name (str): Name of the existing project to modify.
        event.body.created_at (str): UTC datetime string of project creation.
        event.body.exposure_index (int):
            Index of the most recently completed exposure.
        event.body.base_filename (str):
            New filename to add to the project's data.

    Returns:
        200 status code if project succesfully updates with image data.
        Otherwise, 500 status code if project does not unsuccessfully update.
    """

    event_body = json.loads(event.get("body", ""))
    table = dynamodb.Table(projects_table)

    print("event")
    print(json.dumps(event))

    # Unique project identifier
    project_name = event_body["project_name"]
    created_at = event_body["created_at"]

    # Indices for where to save the new data in project_data
    exposure_index = event_body["exposure_index"]

    # Data to save
    base_filename = event_body["base_filename"]

    # First, get the 'project_data' and 'remaining' arrays we want to update.
    # 'project_data[exposure_index]' stores filenames of completed exposures.
    # 'remaining[exposure_index]' is the number of exposures remaining.
    resp1 = table.get_item(
        Key={
            "project_name": project_name,
            "created_at": created_at,
        }
    ) 
    project_data = resp1["Item"]["project_data"]
    remaining = resp1["Item"]["remaining"]

    # Next, add our new information
    project_data[exposure_index].append(base_filename)

    # Floored at zero. The observatory can and does deliver more frames than
    # an exposure asked for -- the sequencer cycles its whole exposure list
    # once per unit of `left_to_do`, which it seeds with the SUM of every
    # exposure's count, so a project with several entries has each of them
    # taken sum(counts) times rather than its own count. Without the floor
    # `remaining` then ran negative without limit and stopped being usable as
    # a progress figure: it is read as "how many still to take", and -90 does
    # not answer that question.
    #
    # The extra frames are still recorded in project_data above, so nothing is
    # lost -- the count of files there remains the honest record of what was
    # actually captured, and the difference between the two is how you would
    # spot the over-exposure.
    remaining[exposure_index] = max(0, int(remaining[exposure_index]) - 1)

    print("updated values: ")
    print(project_data)
    print(remaining)
    
    # Finally, update the DynamoDB project entry with 
    # the revised 'project_data' and 'remaining'
    resp2 = table.update_item(
        Key={
            "project_name": project_name,
            "created_at": created_at,
        },
        UpdateExpression="SET #project_data = :project_data_updated, #remaining = :remaining_updated",
        ExpressionAttributeNames={
            "#project_data": "project_data",
            "#remaining": "remaining",
        },
        ExpressionAttributeValues={
            ":project_data_updated": project_data,
            ":remaining_updated": remaining,
        }
    )
    if resp2["ResponseMetadata"]["HTTPStatusCode"] == 200:
        return create_response(200, json.dumps({"message": "success"}))
    else:
        return create_response(500, json.dumps({"message": "failed to update project in dynamodb"}))


def deleteProject(event, context):
    """Deletes a project from the DynamoDB table.

    A user can only delete their own projects. Only admins can delete
    the projects of other users, so the user's role must be checked first.

    Args:
        event.body.project_name (str): name of the project we want to delete.
        event.body.created_at (str): UTC datetime string of project creation.
        context.requestContext.authorizor.principalID (str):
            Auth0 user 'sub' token (eg. 'google-oauth2|xxxxxxxxxxxxx').
        context.requestContext.authorizer.userRoles (str):
            Global user account type (eg. 'admin') of the requesting user.

    Returns:
        200 status code with successful projection deletion.
        Otherwise, 403 status code if requesting user is unauthorized.
    """
    
    request_body = json.loads(event.get("body", ""))
    table = dynamodb.Table(projects_table)

    print("event")
    print(json.dumps(event))

    # Get the user's roles provided by the lambda authorizer
    userMakingThisRequest = event["requestContext"]["authorizer"]["principalId"]
    print(f"userMakingThisRequest: {userMakingThisRequest}")
    userRoles = json.loads(event["requestContext"]["authorizer"]["userRoles"])
    print(f"userRoles: {userRoles}")

    # Check if the requester is an admin
    # A real boolean. This was the string "false", which is truthy, so the
    # authorization test below passed for everybody -- see there.
    requester_is_admin = 'admin' in userRoles
    requesterIsAdmin = "true" if requester_is_admin else "false"
    print(f"requesterIsAdmin: {requesterIsAdmin}")

    # Specify the event with our pk (project_name) and sk (created_at)
    project_name = request_body['project_name']
    created_at = request_body['created_at']

    # Get the project we want to delete so we can remove it from all its
    # scheduled calendar events.
    event_response = table.get_item(
        Key={
            "project_name": project_name,
            "created_at": created_at
        },
    )
    # A project that is not there cannot be deleted, and subscripting ['Item']
    # for one raised KeyError and surfaced as a 500.
    if 'Item' not in event_response:
        return create_response(404, "No project found to delete.")
    project = event_response['Item']
    # .get: a project stored before new-project defaulted this field has no
    # scheduled_with_events at all, and those records could not be deleted
    # through this endpoint because of it.
    associated_events = project.get('scheduled_with_events', [])

    # Authorize BEFORE touching anything. The test here used to be
    #     if requesterIsAdmin or userMakingThisRequest == ...
    # against the STRING "false", which is truthy -- so it passed for every
    # caller and the comment above it described the opposite of what happened.
    # A stranger's delete was refused by the conditional write further down,
    # but only after this had already unregistered the project from all of its
    # calendar bookings: the project survived, stripped of its schedule, and
    # nothing said so.
    if not (requester_is_admin or userMakingThisRequest == project.get('user_id')):
        msg = "You may only delete your own projects."
        print(msg)
        return create_response(403, msg)

    print("removing projects from calendar events: ")
    print(associated_events)
    removeProjectFromCalendarEvents(associated_events)

    try:
        # Now we can delete the item
        response = table.delete_item(
            Key={
                "project_name": project_name,
                "created_at": created_at
            },
            ConditionExpression=":requesterIsAdmin = :true OR user_id = :requester_id",
            ExpressionAttributeValues = {
                ":requester_id": userMakingThisRequest, 
                ":requesterIsAdmin": requesterIsAdmin,
                ":true": "true"
            }
        )
    
    except ClientError as e:
        print(f"error deleting project: {e}")
        if e.response['Error']['Code'] == "ConditionalCheckFailedException":
            print(e.response['Error']['Message'])
            return create_response(403, "You may only delete your own projects.")
        return create_response(403, e.response['Error']['Message'])
    
    message = json.dumps(response, indent=4, cls=DecimalEncoder)
    print(f"success deleting project; message: {message}")
    return create_response(200, message)


def deleteSchedulerProjects(event, context):
    """Special delete method: intended only for clearing expired scheduler outputs
    
    Args:
        event.body.project_ids (list or str): IDs for all projects to delete.
            Each ID is formatted {project_name}#{created_at}.
    
    Returns:
        200 status code with successful project deletion and the following:
            successful_delete_count: number of projects deleted successfully
            failed_delete_count: number of projects that failed to delete
            failed_ids: list of IDs for projects that failed to delete
    """
    request_body = json.loads(event.get("body", "{}"))
    
    table = dynamodb.Table(projects_table)
    ids_to_delete = request_body.get("project_ids", [])
    failed_to_delete = []

    for id in ids_to_delete:
        project_name, created_at = id.split("#", 1)
        try:
            table.delete_item(
                Key={
                    "project_name": project_name,
                    "created_at": created_at
                },
                ConditionExpression="origin = :scheduler_origin",
                ExpressionAttributeValues={
                    ":scheduler_origin": "LCO"
                }
            )
        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            print(f"Error deleting project {id}: {e}")
            if error_code == "ConditionalCheckFailedException":
                print(f"Failed to delete project {id} because origin is not LCO.")
            failed_to_delete.append(id)

    response_message = {
        "successful_delete_count": len(ids_to_delete) - len(failed_to_delete),
        "failed_delete_count": len(failed_to_delete),
        "failed_ids": failed_to_delete,
        "message": "Delete finished"
    }
    return create_response(200, json.dumps(response_message))

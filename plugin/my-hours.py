from getpass import getpass # XXX: Maybe this too
import toml
import datetime
import requests
from zoneinfo import ZoneInfo

from slugify import slugify

from faff_core.models import Plan, Log, Timesheet, TimesheetMeta
from faff_core.plugins import PlanSource, Audience

class MyHoursPlugin(PlanSource, Audience):

    def initialise_auth(self):
        # FIXME: Should this be using typer?
        print("Please enter your password to authenticate with MyHours.")
        print("This password will not be stored.")
        print(f"User: {self.config.get('email')}")

        LOGIN_API = "https://api2.myhours.com/api/tokens/login"

        body = {
            "granttype": "password",
            "email": self.config.get('email'),
            "password": getpass(),
            "clientId": "api"
        }

        data = requests.post(LOGIN_API, json=body)
        if data.status_code == 200:
            expires_at_dt = datetime.datetime.now(ZoneInfo("UTC")) + datetime.timedelta(seconds=data.json().get('expiresIn'))
            auth = {
                "access_token": data.json().get('accessToken'),
                "refresh_token": data.json().get('refreshToken'),
                "expires_in": data.json().get('expiresIn'),
                "expires_at": expires_at_dt
            }
            # Write to file with isoformat string
            auth_to_save = auth.copy()
            auth_to_save["expires_at"] = expires_at_dt.isoformat()
            (self.state_path / 'token.toml').write_text(toml.dumps(auth_to_save))
            return auth
        elif data.status_code == 401:
            raise ValueError("Invalid credentials. Please check your email and password.")
        else:
            raise ValueError("An error occurred during authentication.")

    def refresh_if_necessary(self, auth):
        if datetime.datetime.now(ZoneInfo("UTC")) > auth['expires_at'] - datetime.timedelta(minutes=5):
            print("Refreshing MyHours token...")
            REFRESH_API = "https://api2.myhours.com/api/tokens/refresh"
            body = {
                "granttype": "refresh_token",
                "refreshToken": auth['refresh_token']
            }

            headers = {"Authorization": f"Bearer { auth['access_token'] }"}

            refresh = requests.post(REFRESH_API, json=body, headers=headers)
            if refresh.status_code == 200:
                expires_at_dt = datetime.datetime.now(ZoneInfo("UTC")) + datetime.timedelta(seconds=refresh.json().get('expiresIn'))
                new_auth = {
                    "access_token": refresh.json().get('accessToken'),
                    "refresh_token": refresh.json().get('refreshToken'),
                    "expires_in": refresh.json().get('expiresIn'),
                    "expires_at": expires_at_dt
                }
                # Write to file with isoformat string
                new_auth_to_save = new_auth.copy()
                new_auth_to_save["expires_at"] = expires_at_dt.isoformat()
                (self.state_path / 'token.toml').write_text(toml.dumps(new_auth_to_save))
                return new_auth
            elif refresh.status_code == 401:
                (self.state_path / 'token.toml').unlink(missing_ok=True)
                raise ValueError("Invalid refresh token. You will have to re-authenticate.")
            else:
                raise ValueError("An error occurred during token refresh.")
        else:
            # Token is still valid, no need to refresh
            return auth

    def authenticate(self):
        """
        Authenticate with MyHours API using the provided email and token.
        This method is a placeholder and should be implemented based on the
        specific authentication requirements of the MyHours API.
        """
        token_state_path = self.state_path / 'token.toml'
        try:
            loaded_toml = toml.loads(token_state_path.read_text())
            auth = {
                "access_token": loaded_toml.get('access_token'),
                "refresh_token": loaded_toml.get('refresh_token'),
                "expires_in": loaded_toml.get('expires_in'),
                "expires_at": datetime.datetime.fromisoformat(loaded_toml.get('expires_at', ''))
            }

        except FileNotFoundError:
            auth = self.initialise_auth()

        try:
            auth = self.refresh_if_necessary(auth)
        except ValueError as e:
            # If refresh token is invalid/expired, re-authenticate
            if "Invalid refresh token" in str(e):
                print("Your session has expired. Please log in again.")
                auth = self.initialise_auth()
            else:
                raise

        return auth.get('access_token')

    def pull_plan(self, date: datetime.date) -> Plan:
        myhours_bearer_token = self.authenticate()
        headers = {"Authorization": f"Bearer {myhours_bearer_token}"}

        print("Pulling MyHours plan...")

        # Pagination setup
        trackers = {}
        subjects = []
        resp = requests.get(
            "https://api2.myhours.com/api/projects",
            headers=headers
        )
        resp.raise_for_status()
        page_data = resp.json()
        for project in page_data:
            trackers[str(project.get('id'))] = project.get('name')

            if project.get('name').lower().startswith('support - '):
                subjects.append(f"customer/{slugify(project.get('name')[len('Support - '):])}")
    
        return Plan(
            source=self.id,
            valid_from=date,
            valid_until=None,
            roles=self.defaults.get("roles", []),
            objectives=self.defaults.get("objectives", []),
            actions=self.defaults.get("actions", []),
            subjects=self.defaults.get("subjects", []) + subjects,
            trackers=trackers
        )

    def compile_time_sheet(self, log: Log) -> Timesheet | None:
        # Only include sessions that have trackers from this plugin's source
        # e.g., if self.id is "element", only include sessions with trackers starting with "element:"
        def has_tracker_for_this_source(session):
            if not session.intent.trackers:
                return False
            return any(t.startswith(f'{self.id}:') for t in session.intent.trackers)

        timeline = [x for x in log.timeline if has_tracker_for_this_source(x)]

        # Always create a timesheet, even if empty
        # This allows the system to track that this date has been compiled
        return Timesheet(
            actor=self.config.get('actor', ''),
            signatures={},
            date=log.date,
            compiled=datetime.datetime.now(ZoneInfo("UTC")),
            timezone=log.timezone,
            timeline=timeline,
            meta=TimesheetMeta(
                audience_id=self.id,
                submitted_at=None
            )
        )

    def get_myhours_day(self, date: datetime.date) -> dict:
        myhours_bearer_token = self.authenticate()
        headers = {"Authorization": f"Bearer {myhours_bearer_token}"}

        response = requests.get(
            "https://api2.myhours.com/api/Logs",
            headers=headers,
            params={
                "date": date.isoformat(),  # FIXME: This feels vulnerable to timezone issues
                "step": "100"
            }
        )
        response.raise_for_status()
        return response.json()
    
    def check_day_empty(self, date: datetime.date) -> bool:
        myhours_day = self.get_myhours_day(date)
        return myhours_day == []
    
    def vape_myhours_day(self, date: datetime.date) -> None:
        day = self.get_myhours_day(date)
        if not day:
            print(f"No existing MyHours entries for {date}")
            return

        print(f"Found {len(day)} existing MyHours entry/entries for {date}:")
        for mh_log in day:
            log_id = mh_log.get('id')
            project = mh_log.get('projectName', 'Unknown project')
            note = mh_log.get('note', 'No note')
            duration = mh_log.get('duration', 0)
            hours = duration / 3600 if duration else 0
            print(f"  Deleting: [{project}] {note} ({hours:.2f}h) [ID: {log_id}]")
            self.delete_myhours_log(log_id)

    def insert_myhours_log(self, thing) -> None:
        """
        Inserts a log entry into MyHours.

        Args:
            thing (Dict[str, Any]): The log entry to insert.
        """
        myhours_bearer_token = self.authenticate()
        headers = {"Authorization": f"Bearer {myhours_bearer_token}"}

        response = requests.post(
            "https://api2.myhours.com/api/Logs/insertlog",
            json=thing,
            headers=headers
        )

        if response.status_code >= 400:
            print(f"Error inserting log entry. Status: {response.status_code}")
            print(f"Request data: {thing}")
            print(f"Response: {response.text}")

            # Check for specific error cases
            if response.status_code == 400:
                try:
                    error_data = response.json()
                    # Check both message and validationErrors array for archived project error
                    validation_errors = error_data.get("validationErrors", [])
                    error_text = error_data.get("message", "").lower()
                    for err in validation_errors:
                        error_text += " " + str(err).lower()

                    if "archived project" in error_text:
                        print(f"Skipping entry for archived project {thing.get('projectId')}")
                        # FIXME: We should mark the timesheet as partially submitted or track
                        # that this entry couldn't be pushed. Currently the timesheet will be
                        # marked as fully submitted even though this entry was skipped.
                        # Options: add metadata to timesheet about failed entries, or don't
                        # mark as submitted if any entries fail.
                        return  # Skip this entry instead of crashing
                except:
                    pass

        response.raise_for_status()

    def delete_myhours_log(self, myhours_log_id: int) -> None:
        """
        Deletes a log entry from MyHours.

        Args:
            log_id (int): The ID of the log entry to delete.
        """
        myhours_bearer_token = self.authenticate()
        headers = {"Authorization": f"Bearer {myhours_bearer_token}"}

        response = requests.delete(
            f"https://api2.myhours.com/api/Logs/{myhours_log_id}",
            headers=headers
        )
        response.raise_for_status()

    def submit_timesheet(self, timesheet: Timesheet) -> None:
        """
        Pushes a compiled timesheet to a remote repository.

        Args:
            config (Dict[str, Any]): Configuration specific to the destination.
            timesheet (Dict[str, Any]): The compiled timesheet to push.
        """
        # Skip submitting empty timesheets
        if not timesheet.timeline:
            print(f"Skipping submission for {timesheet.date} - no sessions to submit")
            return

        print(f"\nSubmitting timesheet for {timesheet.date}...")
        self.vape_myhours_day(timesheet.date)

        print(f"\nInserting {len(timesheet.timeline)} entry/entries:")
        for item in timesheet.timeline:
            # Validate trackers exist and are not empty
            if not item.intent.trackers or len(item.intent.trackers) == 0:
                print(f"Warning: Skipping timeline item '{item.intent.alias}' - no trackers found")
                continue

            # Extract tracker ID, handling the 'element:' prefix if present
            tracker_raw = item.intent.trackers[0]
            if tracker_raw.startswith('element:'):
                tracker = tracker_raw[len('element:'):]
            else:
                # If no prefix, use as-is (for backwards compatibility or other tracker formats)
                tracker = tracker_raw

            # Calculate duration for display
            duration_seconds = (item.end - item.start).total_seconds()
            hours = duration_seconds / 3600

            myhours_log = {
                "projectId": tracker,
                "note": f"{item.intent.alias}",
                "date": item.start.strftime("%Y-%m-%d"),
                "start": item.start.astimezone(ZoneInfo("UTC")).isoformat(),
                "end": item.end.astimezone(ZoneInfo("UTC")).isoformat(),
            }

            print(f"  Inserting: [Project {tracker}] {item.intent.alias} ({hours:.2f}h)")
            self.insert_myhours_log(myhours_log)
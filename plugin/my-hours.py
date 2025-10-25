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
            auth = {
                "access_token": data.json().get('accessToken'),
                "refresh_token": data.json().get('refreshToken'),
                "expires_in": data.json().get('expiresIn'),
                "expires_at": (datetime.datetime.now(ZoneInfo("UTC")) + datetime.timedelta(seconds=data.json().get('expiresIn'))).isoformat()
            }
            (self.state_path / 'token.toml').write_text(toml.dumps(auth))
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
                new_auth = {
                    "access_token": refresh.json().get('accessToken'),
                    "refresh_token": refresh.json().get('refreshToken'),
                    "expires_in": refresh.json().get('expiresIn'),
                    "expires_at": (datetime.datetime.now(ZoneInfo("UTC")) + datetime.timedelta(seconds=refresh.json().get('expiresIn'))).isoformat()
                }
                (self.state_path / 'token.toml').write_text(toml.dumps(new_auth))
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

    def compile_time_sheet(self, log: Log) -> Timesheet:
        return Timesheet(
            actor=self.config.get('actor', ''),
            signatures={},
            date=log.date,
            compiled=datetime.datetime.now(ZoneInfo("UTC")),
            timezone=log.timezone,
            timeline=[x for x in log.timeline if x.intent.trackers and len(x.intent.trackers) > 0],
            meta=TimesheetMeta(
                audience_id=self.id,
                submitted_at=None,
                submitted_by=None
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
        for mh_log in day:
            print(mh_log.get('id'))
            self.delete_myhours_log(mh_log.get('id'))

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
        self.vape_myhours_day(timesheet.date)
        for item in timesheet.timeline:
            # Validate trackers exist and are not empty
            if not item.trackers or len(item.trackers) == 0:
                print(f"Warning: Skipping timeline item '{item.alias}' - no trackers found")
                continue

            # Extract tracker ID, handling the 'element:' prefix if present
            tracker_raw = item.trackers[0]
            if tracker_raw.startswith('element:'):
                tracker = tracker_raw[len('element:'):]
            else:
                # If no prefix, use as-is (for backwards compatibility or other tracker formats)
                tracker = tracker_raw

            myhours_log = {
                "projectId": tracker,
                "note": f"{item.alias}",
                "date": item.start.format("YYYY-MM-DD"),
                "start": str(item.start.in_timezone("UTC")),
                "end": str(item.end.in_timezone("UTC")),
            }
            self.insert_myhours_log(myhours_log)
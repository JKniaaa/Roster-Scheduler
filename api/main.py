from datetime import date
from typing import List, Optional, Any, Dict
import pandas as pd
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, model_validator, Field, ConfigDict
import datetime as dt
from src.scheduler.builder import build_schedule_model
from src.utils.validate import validate_data
from src.utils.constants import *
from src.exceptions.custom_errors import *
import re
import traceback
import logging

def toCamel(string: str) -> str:
    """
    Converts a string in snake_case format to camelCase format.

    For example, "hello_world" is converted to "helloWorld".

    :param string: The string to convert
    :return: The converted string
    """
    parts = string.split("_")
    return parts[0] + "".join(p.capitalize() for p in parts[1:])


class CamelModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=toCamel,
        populate_by_name=True,
        extra="allow"
    )

app = FastAPI()

CUSTOM_ERRORS = {
    NoFeasibleSolutionError: 422,
    InvalidMCError: 400,
    ConsecutiveMCError: 400,
    InputMismatchError: 400,
    InvalidPreviousScheduleError: 400,
    InvalidPrioritySettingError: 400
}

# Define data models
class NurseProfile(CamelModel):
    model_config = ConfigDict(extra="allow")

    name: str
    title: str
    years_experience: int

    @model_validator(mode="before")
    @classmethod
    def extract_years_experience(cls, values: Any) -> Any:
        """
        Model validator to extract years_experience from other keys in the input data if not present.

        This validator is needed to handle the case where the column name for years of experience is not exactly "years_experience", e.g. "Years of Experience", "Year(s) Experience", etc.

        If the "years_experience" key is not present, the validator iterates over all the keys in the input data and checks if the key contains the words "year" or "experience". If a matching key is found, the value associated with that key is moved to the "years_experience" key, and the original key is removed from the input data.
        """
        if "years_experience" not in values:
            for key in list(values.keys()):
                lowered = key.lower()
                if "year" in lowered or "experience" in lowered:
                    values["years_experience"] = values.pop(key)
                    break
        return values

class NursePreference(CamelModel):
    model_config = ConfigDict(extra="allow")

    nurse: str
    date: date
    shift: str
    timestamp: Optional[dt.datetime] = None

class NurseTraining(CamelModel):
    model_config = ConfigDict(extra="allow")

    nurse: str
    date: date
    training: str

class PrevSchedule(CamelModel):
    model_config = ConfigDict(extra="allow")
    index: str        # <nurse>
    # <date>: <shift> fields will be handled via internal logic

class FixedAssignment(CamelModel):
    model_config = ConfigDict(extra="allow")

    nurse: str
    date: date
    fixed: str

class ScheduleRequest(CamelModel):
    model_config = ConfigDict(extra="allow")

    start_date: date
    num_days: int
    shift_durations: List[int] = Field(default=SHIFT_DURATIONS)
    min_nurses_per_shift: int = Field(default=MIN_NURSES_PER_SHIFT)
    min_seniors_per_shift: int = Field(default=MIN_SENIORS_PER_SHIFT)
    max_weekly_hours: int = Field(default=MAX_WEEKLY_HOURS)
    preferred_weekly_hours: int = Field(default=PREFERRED_WEEKLY_HOURS)
    pref_weekly_hours_hard: bool = False
    min_acceptable_weekly_hours: int = Field(default=MIN_ACCEPTABLE_WEEKLY_HOURS)
    activate_am_cov: bool = True
    am_coverage_min_percent: int = Field(default=AM_COVERAGE_MIN_PERCENT)
    am_coverage_min_hard: bool = False
    am_coverage_relax_step: int = Field(default=AM_COVERAGE_RELAX_STEP)
    am_senior_min_percent: int = Field(default=AM_SENIOR_MIN_PERCENT)
    am_senior_min_hard: bool = False
    am_senior_relax_step: int = Field(default=AM_SENIOR_RELAX_STEP)
    weekend_rest: bool = True
    back_to_back_shift: bool = False
    use_sliding_window: bool = False
    shift_balance: bool = False
    priority_setting: str = "50/50"
    fixed_assignments: Optional[List[FixedAssignment]] = None


def standardize_profile_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    Standardize the column names of a nurse profile DataFrame to "Name", "Title", and "Years of experience".
    
    The function takes a DataFrame with columns representing nurse names, titles, and years of experience.
    It returns a new DataFrame with the same data, but with standardized column names.
    
    The function first builds a dictionary mapping lower-case, stripped column names to the original column names.
    It then uses this dictionary to find the columns in the DataFrame that match the candidates.
    If no exact match is found, it tries a substring match.
    If no match is found, it raises a ValueError.
    
    The function then copies the relevant columns into a new DataFrame and renames them.
    Finally, it strips and upper-cases the Name column and returns the new DataFrame.
    """
    col_map = {col.lower().strip(): col for col in df.columns}

    def find_col(*candidates: str) -> str:
        # Try exact match first
        for c in candidates:
            if c in col_map:
                return col_map[c]
        # Then try substring match
        for lower, original in col_map.items():
            if any(c in lower for c in candidates):
                return original
        raise ValueError(f"No column matching {candidates} in {list(df.columns)}")

    name_src  = find_col("name")
    title_src = find_col("title")
    exp_src   = find_col("experience", "year")

    out = df[[name_src, title_src, exp_src]].copy()
    out.columns = ["Name", "Title", "Years of experience"]
    out["Name"] = out["Name"].astype(str).str.strip().str.upper()
    return out


class ScheduleEntry(CamelModel):
    index: str
     # all the "Day Date": "Shift" keys flow through as extras

class SummaryEntry(CamelModel):
    index: int      # row index
    nurse: str = Field(..., alias="Nurse")
    # everything else (AL, MC, Hours_Week1_Real, Prefs_Unmet, etc.) passes through

class ScheduleResponse(CamelModel):
    schedule: List[ScheduleEntry]
    summary: List[SummaryEntry]
    violations: Dict[str, Any]
    metrics: Dict[str, Any]

class GenerateSchedulePayload(CamelModel):
    profiles: List[NurseProfile]
    preferences: List[NursePreference]
    training_shifts: List[NurseTraining] = Field(default_factory=list)
    previous_schedule: List[PrevSchedule] = Field(default_factory=list)
    request: ScheduleRequest

@app.post("/schedule/generate/", response_model=ScheduleResponse)
async def generate_schedule(
    payload: GenerateSchedulePayload
):
    """
    Generate a schedule based on the given nurse profiles, shift preferences, and other parameters.

    Parameters
    ----------
    payload : GenerateSchedulePayload
        The input payload containing:
        - profiles : List[NurseProfile]
            Each with:
            - name (str)
            - title (str)
            - yearsExperience (int)

        - preferences : List[NursePreference]
            Each with:
            - nurse (str)
            - date (str, "YYYY-MM-DD")
            - shift (str)
            - timestamp (str, ISO 8601)

        - trainingShifts : List[NurseTraining]
            Each with:
            - nurse (str)
            - date (str, "YYYY-MM-DD")
            - training (str)

        - previousSchedule : List[PrevSchedule]
            Each with:
            - index (str)  — nurse identifier
            - "<Day YYYY‑MM‑DD>" columns for past shifts or leave codes

        - request : ScheduleRequest
            Scheduling parameters:

            - startDate (str, "YYYY-MM-DD")
            - numDays (int)
            - shiftDurations (List[int]) — hours per shift
            - minNursesPerShift (int)
            - minSeniorsPerShift (int)
            - maxWeeklyHours (int)
            - preferredWeeklyHours (int)
            - minAcceptableWeeklyHours (int)
            - prefWeeklyHoursHard (bool)
            - activateAmCov (bool)
            - amCoverageMinPercent (int)
            - amCoverageMinHard (bool)
            - amCoverageRelaxStep (int)
            - amSeniorMinPercent (int)
            - amSeniorMinHard (bool)
            - amSeniorRelaxStep (int)
            - weekendRest (bool)
            - backToBackShift (bool)
            - useSlidingWindow (bool)
            - shiftBalance (bool)
            - prioritySetting (str)  — only active when `shiftBalance` is `True`
            - fixedAssignments : List[FixedAssignment]
                Each with:
                - nurse (str)
                - date (str, "YYYY-MM-DD")
                - fixed (str)

    Returns
    -------
    ScheduleResponse
        A JSON object with:

        - schedule : List[Dict[str, str]]
            Each mapping:
            - index (str)
            - "<Day YYYY-MM-DD>" : assigned shift or leave code

        - summary : List[Dict[str, Any]]
            Each summary row contains:
            - index (int)
            - Nurse (str)
            - counts for AL, MC, EL, Rest, AM, PM, Night, training, Double Shifts
            - Hours_Week1_Real, Hours_Week1_InclAL, Hours_Week2_Real, Hours_Week2_InclAL (int)
            - Prefs_Met (int)
            - Prefs_Unmet (int)
            - Unmet_Details (str)

        - violations : Dict[str, List[Any]]
            Keys are constraint names (e.g. "Low Hours Nurses").

        - metrics : Dict[str, Any]
            - PreferenceMet (int)
            - PreferenceUnmet (List[str])
            - FairnessGap (int)

    Raises
    ------
    HTTPException

        - 400 Bad Request: invalid input or parsing errors
        - 422 Unprocessable Entity: no feasible solution
        - 500 Internal Server Error: unexpected exceptions
    """
    profiles = payload.profiles
    preferences = payload.preferences
    training_shifts = payload.training_shifts
    previous_schedule = payload.previous_schedule
    request = payload.request

    try:
        # Convert array inputs to raw DataFrame
        raw = pd.DataFrame([p.model_dump() for p in profiles])
        # Standardize it to exactly Name/Title/Years of experience
        profiles_df = standardize_profile_columns(raw)

        # Handle preferences
        if preferences:
            # 1) build raw DataFrame
            pref_df = pd.DataFrame([p.model_dump() for p in preferences])

            if 'timestamp' in pref_df.columns:
                # 2) coerce to datetime & sort so earliest come first
                pref_df['timestamp'] = pd.to_datetime(pref_df['timestamp'])
                pref_df.sort_values(
                    by=['date','shift','timestamp'],
                    ascending=[True, True, True],
                    inplace=True
                )

            # 3) pack shift+ts into one column
            pref_df['cell'] = list(zip(pref_df['shift'], pref_df['timestamp']))

            # 4) drop any duplicate nurse+date, keeping that earliest row, then pivot
            prefs_df = (
                pref_df
                .drop_duplicates(subset=['nurse','date'], keep='first')
                .pivot(index='nurse', columns='date', values='cell')
                .rename_axis(None, axis=0)
                .rename_axis(None, axis=1)
            )

            # normalize to match profiles_df["Name"]
            prefs_df.index = prefs_df.index.str.strip().str.upper()
            prefs_df.index.name = "Name"

        else:
            prefs_df = pd.DataFrame(index=profiles_df["Name"].str.upper())
            prefs_df.index.name = "Name"

        # Handle training shifts
        if training_shifts:
            raw_train = pd.DataFrame([t.model_dump() for t in training_shifts])
            training_df = (
                raw_train
                .pivot(index='nurse', columns='date', values='training')
            )
            # normalize to match profiles_df["Name"]
            training_df.index = (
                training_df
                .index
                .astype(str)
                .str.strip()
                .str.upper()
            )
            training_df.index.name = "Name"
        else:
            # build an empty table with the same normalized index
            training_df = pd.DataFrame(index=profiles_df["Name"])
            training_df.index.name = "Name"

        if previous_schedule:
            prev_sched_df = pd.DataFrame([p.model_dump() for p in previous_schedule])
            # set nurse index
            if "index" not in prev_sched_df.columns:
                raise HTTPException(400, detail="Each prev_schedule row requires an 'index' field")
            prev_sched_df = prev_sched_df.set_index("index")
            prev_sched_df.index = prev_sched_df.index.astype(str).str.strip().str.upper()
            prev_sched_df.index.name = "Name"
            # robust date‑column parsing
            converted = {}
            for col in prev_sched_df.columns:
                m = re.search(r"\d{4}-\d{2}-\d{2}", col)
                if not m:
                    raise HTTPException(400, detail=f"Could not parse date in prev-schedule column '{col}'")
                converted[col] = pd.to_datetime(m.group(0))
            prev_sched_df = prev_sched_df.rename(columns=converted)
            prev_schedule_df = prev_sched_df
        else:
            prev_schedule_df = pd.DataFrame(index=profiles_df["Name"])
            prev_schedule_df.index.name = "Name"

        # Ensure indices are string type for validation
        if not prefs_df.empty and prefs_df.index.dtype != 'object':
            prefs_df.index = prefs_df.index.astype(str)
        if not training_df.empty and training_df.index.dtype != 'object':
            training_df.index = training_df.index.astype(str)
        
        # === Execute the original validation ===
        validate_data(profiles_df, prefs_df, "profiles", "preferences", False)
        validate_data(profiles_df, training_df, "profiles", "training shifts", False)
        validate_data(profiles_df, prev_schedule_df, "profiles", "previous schedule", False)
        
        # Handle fixed assignments
        fixed_assignments_dict = None
        if request.fixed_assignments:
            # build a dict keyed by (nurse, date)
            fixed_assignments_dict = {(fa.nurse, fa.date): fa.fixed 
                                    for fa in request.fixed_assignments}
            # now convert dates to day‐indices
            fixed_idx_dict = {}
            for (nurse, dt), shift in fixed_assignments_dict.items():
                idx = (pd.Timestamp(dt) - pd.Timestamp(request.start_date)).days
                if idx < 0 or idx >= request.num_days:
                    raise HTTPException(400,
                        f"Fixed assignment for {nurse} on {dt} is outside the scheduling window")
                fixed_idx_dict[(nurse, idx)] = shift
            
        # Convert hours→minutes so the CP‑SAT model sees minutes everywhere
        dur_minutes = [h * 60 for h in request.shift_durations]
        
        # Call scheduling function

        # # Log the inputs for debugging
        # logging.info("=== API inputs for build_schedule_model ===")
        # logging.info("profiles_df.shape:         %s", profiles_df.shape)
        # logging.info("profiles_df.columns:       %s", profiles_df.columns.tolist())
        # logging.info("prefs_df.shape:            %s, index sample: %r",
        #              prefs_df.shape, list(prefs_df.index)[:5])
        # logging.info("prefs_df.columns sample:   %r", list(prefs_df.columns)[:5])
        # logging.info("training_df.shape:         %s, index sample: %r",
        #              training_df.shape, list(training_df.index)[:5])
        # logging.info("training_df.columns sample:%r", list(training_df.columns)[:5])
        # logging.info("prev_schedule_df.shape:    %s, index sample: %r",
        #              prev_schedule_df.shape, list(prev_schedule_df.index)[:5])
        # logging.info("start_date:                %s", request.start_date)
        # logging.info("num_days:                  %d", request.num_days)
        # logging.info("shift_durations (hrs):     %r", request.shift_durations)
        # logging.info("shift_durations (mins):    %r", dur_minutes)
        # logging.info("min_nurses_per_shift:      %d", request.min_nurses_per_shift)
        # logging.info("min_seniors_per_shift:     %d", request.min_seniors_per_shift)
        # logging.info("max_weekly_hours (hrs):    %d", request.max_weekly_hours)
        # logging.info("max_weekly_hours (mins):   %d", request.max_weekly_hours * 60)
        # logging.info("preferred_weekly_hours (hrs):  %d", request.preferred_weekly_hours)
        # logging.info("preferred_weekly_hours (mins): %d", request.preferred_weekly_hours * 60)
        # logging.info("min_accept_weekly_hours (hrs):  %d", request.min_acceptable_weekly_hours)
        # logging.info("min_accept_weekly_hours (mins): %d", request.min_acceptable_weekly_hours * 60)
        # logging.info("activate_am_cov:           %s", request.activate_am_cov)
        # logging.info("am_coverage_min_percent:   %d", request.am_coverage_min_percent)
        # logging.info("am_coverage_min_hard:      %s", request.am_coverage_min_hard)
        # logging.info("am_senior_min_percent:     %d", request.am_senior_min_percent)
        # logging.info("am_senior_min_hard:        %s", request.am_senior_min_hard)
        # logging.info("weekend_rest:              %s", request.weekend_rest)
        # logging.info("back_to_back_shift:        %s", request.back_to_back_shift)
        # logging.info("use_sliding_window:        %s", request.use_sliding_window)
        # logging.info("shift_balance:             %s", request.shift_balance)
        # logging.info("fixed_assignments count:   %s", len(fixed_assignments_dict or {}))

        schedule, summary, violations, metrics = build_schedule_model(
            profiles_df=profiles_df,
            preferences_df=prefs_df,
            training_shifts_df=training_df,
            prev_schedule_df=prev_schedule_df,
            start_date=pd.Timestamp(request.start_date),
            num_days=request.num_days,
            shift_durations=dur_minutes,
            min_nurses_per_shift=request.min_nurses_per_shift,
            min_seniors_per_shift=request.min_seniors_per_shift,
            max_weekly_hours=request.max_weekly_hours,
            preferred_weekly_hours=request.preferred_weekly_hours,
            pref_weekly_hours_hard=request.pref_weekly_hours_hard,
            min_acceptable_weekly_hours=request.min_acceptable_weekly_hours,
            activate_am_cov=request.activate_am_cov,
            am_coverage_min_percent=request.am_coverage_min_percent,
            am_coverage_min_hard=request.am_coverage_min_hard,
            am_coverage_relax_step=request.am_coverage_relax_step,
            am_senior_min_percent=request.am_senior_min_percent,
            am_senior_min_hard=request.am_senior_min_hard,
            am_senior_relax_step=request.am_senior_relax_step,
            weekend_rest=request.weekend_rest,
            back_to_back_shift=request.back_to_back_shift,
            use_sliding_window=request.use_sliding_window,
            shift_balance=request.shift_balance,
            priority_setting=request.priority_setting,
            fixed_assignments=fixed_idx_dict if fixed_assignments_dict else None
        )

        # Convert DataFrames to JSON-friendly format
        raw = {
            "schedule": schedule.reset_index().to_dict(orient="records"),
            "summary": summary.reset_index().to_dict(orient="records"),
            "violations": violations,
            "metrics": metrics
        }
        
        return ScheduleResponse(**raw)
        
    except tuple(CUSTOM_ERRORS) as e:
        raise HTTPException(status_code=CUSTOM_ERRORS[type(e)], detail=str(e))
    except Exception as e:
        tb = traceback.format_exc()
        raise HTTPException(status_code=500, detail=f"{str(e)}\n\nTraceback:\n{tb}")
    

@app.get("/")
def root():
    """
    Simple root endpoint to indicate the API is running.

    Returns a JSON response with a "message" key, containing a string
    indicating the API is running and providing a pointer to the Swagger UI
    documentation at /docs.
    """
    return {"message": "Nurse Roster Scheduling API is running. Visit /docs for the Swagger UI."}

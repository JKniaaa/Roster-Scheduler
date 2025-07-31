schedule_roster_description = """
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
            - "<Day YYYY-MM-DD>" columns for past shifts or leave codes

        - request : ScheduleRequest
            Scheduling parameters:

            - startDate (str, "YYYY-MM-DD")
            - numDays (int)
            - shiftDurations (List[int]) — hours per shift
            - minNursesPerShift (int)
            - minSeniorsPerShift (int)
            - maxWeeklyHours (int)
            - preferredWeeklyHours (int)
            - prefWeeklyHoursHard (bool)
            - minAcceptableWeeklyHours (int)
            - minWeeklyRest (int)
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
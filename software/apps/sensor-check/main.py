"""Read-only sensor example; the SDK shares the robot's existing samplers."""
from robbi.errors import BusyError, UnavailableError


def _state(sample):
    if sample.get("enabled") is False:
        return "disabled"
    if not sample.get("available"):
        return "unavailable"
    return "fresh" if sample.get("fresh") else "stale"


def _range_line(label, sample):
    state = _state(sample)
    if state == "fresh" and sample.get("distance_cm") is not None:
        return f"{label}: {sample['distance_cm']:g} cm"
    return f"{label}: {state if state != 'fresh' else 'no valid range'}"


def run(context, tool_name, arguments):
    needed = ["robot.tof", "robot.imu"]
    if arguments.get("show_on_screen", False):
        needed.append("robot.display")
    context.request_permissions(*needed)
    ranges = {sensor: context.robot.tof(sensor) for sensor in ("h1", "h2")}
    imu = context.robot.imu()
    lines = [_range_line("H1 front", ranges["h1"]), _range_line("H2 auxiliary", ranges["h2"])]
    state, orientation = _state(imu), imu.get("orientation_deg")
    if state == "fresh" and isinstance(orientation, dict) and all(
            isinstance(orientation.get(axis), (int, float)) for axis in ("roll", "pitch", "yaw")):
        lines.append("IMU: roll {roll:.1f}, pitch {pitch:.1f}, relative yaw {yaw:.1f} deg".format(**orientation))
    elif state == "fresh":
        lines.append("IMU: fresh acceleration/gyro; orientation not ready")
    else:
        lines.append(f"IMU: {state}")
    if state == "fresh" and imu.get("calibration") not in {None, "complete"}:
        lines.append("Gyro calibration: " + str(imu["calibration"])[:40])
    lines.append("Relative yaw is not a compass bearing.")
    summary = "\n".join(lines)
    required = [ranges["h1"], imu]
    if ranges["h2"].get("enabled") is not False:
        required.append(ranges["h2"])
    ready = all(sample.get("fresh") for sample in required)
    result = {"ok": ready, "text": summary,
              "data": {"tof": ranges, "imu": imu, "all_sensors_fresh": ready, "display_requested": False}}
    if arguments.get("show_on_screen", False):
        try:
            output = context.robot.display_text(summary, hold_s=10)
        except (BusyError, UnavailableError) as error:
            context.checkpoint()
            output = {"displayed": False, "reason": str(error)}
        result["data"].update(display_requested=True, display=output)
        if output.get("artifact"):
            result["artifacts"] = [output["artifact"]]
        if not output.get("displayed"):
            result["text"] += "\nScreen: " + (output.get("reason") or "not displayed")
    return result

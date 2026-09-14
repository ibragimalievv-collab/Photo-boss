from datetime import date

DAILY_POSE_COUNT = 5
DAILY_BONUS = 500
WEEKLY_BONUS = 10000


def create_daily_training():
    return {
        "count": DAILY_POSE_COUNT,
        "type": "pose_training",
        "created": date.today().isoformat(),
    }


def analyze_shoot_result(studied_poses, shoot_poses):
    studied = set(studied_poses)
    used = studied.intersection(set(shoot_poses))
    return {
        "used": list(used),
        "missing": list(studied - used),
        "score": round(len(used) / len(studied) * 100, 1) if studied else 0,
    }


def build_feedback(errors):
    return [
        f"Исправить: {item}" for item in errors
    ]

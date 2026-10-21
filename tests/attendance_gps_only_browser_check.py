"""Real mobile GPS + camera recovery with hotel reference points deferred.

Synthetic location/camera and disposable users only; no production requests.
"""
import asyncio

from attendance_no_gps_browser_check import main

if __name__ == '__main__':
    asyncio.run(main(gps_only=True))

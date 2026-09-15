import asyncio

from aiohttp import web


async def health(_request):
    return web.Response(text="ok")


async def telegram(request):
    method = request.match_info["method"].lower()
    if method == "getme":
        result = {
            "id": 123456,
            "is_bot": True,
            "first_name": "Photo Boss CI",
            "username": "photo_boss_ci_bot",
        }
    elif method == "getupdates":
        await asyncio.sleep(0.25)
        result = []
    else:
        result = True
    return web.json_response({"ok": True, "result": result})


app = web.Application()
app.router.add_get("/health", health)
app.router.add_post("/bot{token}/{method}", telegram)


if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=8080)

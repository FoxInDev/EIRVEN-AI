from eirven_ai.applications import ApplicationService, InstalledApplication


class DummyBrowser:
    def search(self, query):
        return {"query": query}


def test_resolve_application_generic_fuzzy():
    service = ApplicationService(DummyBrowser())
    service._cache = [
        InstalledApplication("Minecraft Launcher", "minecraft.app"),
        InstalledApplication("Telegram Desktop", "telegram.app"),
    ]
    app = service.resolve("Minecraft Launch")
    assert app.name == "Minecraft Launcher"


def test_legal_movie_search_never_builds_piracy_query():
    service = ApplicationService(DummyBrowser())
    result = service.search_legal_movie("Dune", free_only=True)
    assert "легально" in result["query"]
    assert "бесплатно" in result["query"]

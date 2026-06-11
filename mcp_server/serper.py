"""MCP server exposing Google Search via the Serper API.

Run standalone for testing:  python ./mcp_server/serper.py
The ADK agent launches it over stdio (see main.py).
Requires SERPER_API_KEY in the project-root .env.
"""
import os
import requests
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

SERPER_API_KEY = os.getenv("SERPER_API_KEY")
SERPER_URL = "https://google.serper.dev/search"

mcp = FastMCP("serper")


@mcp.tool()
def google_search(query: str, num_results: int = 5) -> str:
    """Search Google via the Serper API and return the top results.

    Args:
        query: The search query.
        num_results: How many organic results to return (default 5).
    """
    if not SERPER_API_KEY:
        return "SERPER_API_KEY is not set in your .env."

    try:
        resp = requests.post(
            SERPER_URL,
            headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"},
            json={"q": query, "num": num_results},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.RequestException as e:
        return f"Serper request failed: {e}"

    # Surface a direct answer box if Serper returns one
    lines = []
    answer = data.get("answerBox", {})
    if answer:
        snippet = answer.get("answer") or answer.get("snippet")
        if snippet:
            lines.append(f"Answer: {snippet}")

    results = data.get("organic", [])
    if not results and not lines:
        return f"No results found for: {query}"

    for i, r in enumerate(results[:num_results], 1):
        lines.append(
            f"{i}. {r.get('title', '')}\n   {r.get('link', '')}\n   {r.get('snippet', '')}"
        )
    return "\n\n".join(lines)


if __name__ == "__main__":
    mcp.run(transport="stdio")
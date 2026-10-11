import { expect, test } from "@playwright/test"


/** Frame a deterministic provider turn using the browser agent's real SSE path. */
const turn = (delta: object, finish: string): string => {
  const frame = (value: object, reason: string | null) => ({
    id: "fixture", object: "chat.completion.chunk", created: 0, model: "fixture-model",
    choices: [{ index: 0, delta: value, finish_reason: reason }],
  })
  return [frame(delta, null), frame({}, finish)].map((value) => `data: ${JSON.stringify(value)}\n\n`).join("") + "data: [DONE]\n\n"
}


test("select keyless Parallel in settings and search Parallel directly from the browser", async ({ page }) => {
  const source = { url: "https://docs.python.org/3/library/asyncio.html", title: "asyncio", excerpts: ["Python asynchronous I/O"] }
  let searched = false
  let relayed = false
  let resultReceived = false
  let emitted = false
  // The keyless path goes browser → Parallel. `/ai/search` is only the fallback,
  // so a healthy direct call must never reach it.
  await page.route("**/ai/search", async (route) => {
    relayed = true
    await route.fulfill({ json: { status: "success", data: { answer: "", results: [] } } })
  })
  await page.route("https://search.parallel.ai/mcp", async (route) => {
    const request = route.request()
    const body = request.postDataJSON()
    expect(body.method).toBe("tools/call")
    expect(body.params).toEqual({
      name: "web_search",
      arguments: { objective: "Python asyncio documentation", search_queries: ["Python asyncio documentation"] },
    })
    for (const header of ["authorization", "cookie", "x-api-key", "x-provider-key", "x-run-id"]) {
      expect(request.headers()[header]).toBeUndefined()
    }
    searched = true
    await route.fulfill({ json: {
      jsonrpc: "2.0", id: body.id,
      result: { content: [{ type: "text", text: JSON.stringify({ results: [source] }) }], structuredContent: { results: [source] } },
    } })
  })
  await page.route("**/chat/completions", async (route) => {
    const body = route.request().postDataJSON()
    const messages = (body.messages ?? []) as { role: string; content?: string }[]
    if (messages.some((m) => m.role === "tool" && m.content?.includes(source.url))) resultReceived = true
    if (body.stream && body.tools?.length && !emitted) {
      emitted = true
      await route.fulfill({ contentType: "text/event-stream", body: turn({
        role: "assistant", tool_calls: [{ index: 0, id: "search-1", type: "function",
          function: { name: "web_search", arguments: JSON.stringify({ query: "Python asyncio documentation" }) } }],
      }, "tool_calls") })
    } else if (body.stream) {
      await route.fulfill({ contentType: "text/event-stream", body: turn({ role: "assistant", content: "Found asyncio documentation." }, "stop") })
    } else {
      await route.fulfill({ json: { id: "label", object: "chat.completion", created: 0, model: "fixture-model",
        choices: [{ index: 0, finish_reason: "stop", message: { role: "assistant", content: "Search board" } }] } })
    }
  })
  await page.goto("/local")
  await page.getByRole("list", { name: "On this device" }).getByText("New Board").click()
  await page.getByRole("button", { name: "Agent settings" }).click()
  await page.getByRole("button", { name: "Model providers" }).click()
  await page.getByPlaceholder(/sk-/).fill("sk-fixture-model-only")
  await page.getByRole("button", { name: "Save" }).click()
  await page.getByRole("button", { name: /^Web search/ }).click()
  await page.getByRole("button", { name: "Parallel (keyless)" }).click()
  await expect(page.getByText(/No search API key and no sign-in needed/)).toBeVisible()
  await page.getByRole("switch", { name: "Always allow web search", exact: true }).click()
  await page.keyboard.press("Escape")
  await page.reload()
  const saved = await page.evaluate(() => JSON.parse(localStorage.getItem("dim0.byok") ?? "{}"))
  expect(saved.searchEngine).toBe("parallel")
  expect(saved.search).toEqual({})
  await page.getByPlaceholder(/Ask about this board/).fill("Find asyncio documentation")
  await page.getByPlaceholder(/Ask about this board/).press("Enter")
  await expect(page.getByText("Found asyncio documentation.")).toBeVisible()
  expect(searched).toBe(true)
  expect(relayed).toBe(false)
  expect(resultReceived).toBe(true)
})

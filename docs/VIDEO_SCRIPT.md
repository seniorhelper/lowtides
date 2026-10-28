# Demo video script (under 3 minutes)

Record the screen at 1080p with your voice over it. Keep the browser zoom at 100%.
PayPal requires the video to be on YouTube (public) and to show the project actually working.
Use no copyrighted music. Silence or your own voice is safest.

---

**0:00–0:15. The problem (show the Home screen, empty)**
"AI agents are expensive in a way chat never was. A 2026 Stanford study found agent tasks use about a thousand times more tokens than chat, and agents can't predict their own bills. Business owners have nobody watching the meter. So I built a governor."

**0:15–0:40. Setup in three steps**
Type a name, slide the allowance to $5, press **Turn it on**. Show the two lines to paste. Open "Show me code for my agent."
"Name it, give it an allowance, turn it on. Your agent changes one line, the address, and keeps working exactly the same."

**0:40–1:10. Watch it work**
Press **Watch it work (demo)**. Point to the green sign and the plain-English receipt.
"This runs the real pipeline. Only the model at the end is simulated, and it's labeled that way everywhere. Easy steps went to a cheaper model. Two repeat questions were free from memory. Old tool output the agent kept re-reading was trimmed. And the governor never moved it up past the model it asked for."

**1:10–1:30. Try it live (with a real Claude key set)**
In **Try it**, pick `claude-opus-5-5` and ask "Summarize this in one line: our shipment is two days late."
"A real Claude call. It answered on Haiku, a fraction of the Opus price, and here's exactly what it cost and saved."

**1:30–2:10. PayPal: quote, hold, pay only the real cost**
Go to **Quotes & holds**. Get a quote for "Weekly SEO report." Press **Hold with PayPal**, approve in the sandbox, and come back.
Run a few calls with the job header (use the curl in the README), then press **Finish & pay**.
"Like a contractor's estimate. PayPal holds the quote. When the job is done, Lowtides captures only what it really cost and releases the rest. If an account can't do partial captures, it captures and refunds the difference instantly."

**2:10–2:30. Safety: STOP and the loop guard**
Press **Watch it work** with the loop demo (or send the same request five times). Show the agent paused and the alert.
"Agents get stuck. The loop guard pauses them before they burn your budget. And STOP ALL halts every agent instantly."

**2:30–2:50. Business side**
Switch to **Business**. Add a client, assign an agent, **Preview** the invoice, then **Send PayPal invoice**.
"Agencies can run agents for clients and bill them through PayPal, with the savings receipt right on the invoice."

**2:50–3:00. Close**
"Same results, a fraction of the bill, proof in dollars. Open source, built with Claude and PayPal."

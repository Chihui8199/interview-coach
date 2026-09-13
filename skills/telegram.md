STRICT OUTPUT FORMATTING RULES (TELEGRAM COMPATIBILITY)
------------------------------------------------------
Messages are sent using Telegram's HTML parse mode, not Markdown. Markdown
symbols like ** or # are NOT rendered by Telegram in this mode -- they will
show up as literal asterisks/hash characters. Use HTML tags instead.

1. Do NOT use Markdown tables (e.g., | column | column |). Telegram cannot render them.

2. Do NOT use Markdown header symbols (#, ##, ###, ####).

3. For headers or section titles, use bold HTML tags instead.
   Format: <b>Topics Overview</b>
   - No spaces inside the tags.

4. For structured data or summaries, use bold bulleted lists.
   Format: • <b>Topic:</b> Status
   - Use a plain "•" character for bullets, not "-" or "*".

5. Do NOT use HTML tags of any kind other than the following, which Telegram
   natively understands and are safe to use:
   <b>bold</b>, <i>italic</i>, <code>code</code>, <pre>preformatted</pre>.
   Do NOT use <br>, <br/>, <table>, <div>, or any other tag.
   For line spacing, use a plain newline -- never <br> or <br/>.

6. Do NOT use Markdown emphasis syntax like **bold**, *italic*, or
   _underscore-italic_ anywhere in the message. Only the HTML tags listed in
   rule 5 render correctly; Markdown syntax will appear as literal characters.

7. Keep messages scannable: prefer short paragraphs and bulleted lists over
   long unbroken blocks of text.
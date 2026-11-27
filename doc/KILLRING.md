# Kill ring and clipboard design

Status: built, 2026-11-27, all three stages. Section 2 holds the decisions
taken; section 11 says how what was built differs from what is described
before it. Section 3 describes what there was before.

This describes cutting, copying and pasting: the kill ring, how it meets the
system clipboard in both directions, and the chooser that `M-y` opens. It
assumes ENGINE.md (4.5, 5.7, 7.5, 8.5) and FRONTEND.md (5.6, 10).

## 1. Goals and non-goals

Goals:

- A kill ring as in Emacs: everything cut or copied is kept, and any of it
  can be pasted later.
- `C-y` pastes the newest. `M-y` shows the ring and lets the user choose.
- What is cut or copied in Neomacs is on the system clipboard.
- What is cut or copied in another program is in the ring, in the ordinary
  case, without the user doing anything.

Non-goals:

- A clipboard manager. Neomacs does not watch the system clipboard while it
  is in the background (K2).
- Anything but text. Images and rich text on the clipboard are ignored.
- A ring that survives a restart (ENGINE 16, persistent history).

## 2. Decisions taken

| # | Decision | Reason |
|---|---|---|
| K1 | Every kill and copy is its own entry. Consecutive kills are not merged. | Decided by the user, 2026-11-27. Each entry in the chooser is one thing the user did. |
| K2 | The system clipboard is read when the user yanks, when the window regains focus, and when the chooser opens. It is not polled. | Decided by the user. Catches "copy elsewhere, come back" with no timer; does not collect passwords copied while the editor is in the background. |
| K3 | `M-y` opens a chooser: a list of the ring beside a preview of the whole of the highlighted entry. | Decided by the user. A one-line list cannot tell two entries with the same first line apart, or show what a long one holds. |
| K4 | `M-y` straight after a yank replaces what was yanked. At any other time it inserts at the cursor. | Emacs's `M-y` does nothing unless it follows a yank; there is no reason to refuse. |
| K5 | Choosing an entry moves it to the top of the ring and puts it on the system clipboard. | The next `C-y`, here or in another program, pastes what was just chosen. |
| K6 | An entry the same as one already in the ring is not added twice: the existing one moves to the top. | Copying the same thing again should not fill the chooser with it. |
| K7 | The ring belongs to the engine and is shared by every frame (ENGINE D17, unchanged). | One ring, whichever window the text was cut in. |
| K8 | In the chooser, `M-y` again moves down the list. | Decided by the user. Tapping the key walks the ring, as it does in Emacs. |
| K9 | The terminal frontend shows a preview too. | Decided by the user. |
| K10 | `C-d` in the chooser removes the highlighted entry from the ring. | Decided by the user. It is the answer to "I did not mean to keep that". |
| K11 | A kill of nothing but a line break makes an entry, as now. | Decided by the user. By K6 a run of them is one entry. |

## 3. What existed before

- `KillRing` in `neomacs/clipboard.py`: 60 entries; an entry is a list of
  pieces, one for each selection that was killed.
- `kill_selection`, `copy_selection`, `kill_line`, `kill_word` and
  `backward_kill_word` push an entry and send `clipboard.set` to the frame.
- `yank` awaits `clipboard.get`; if the text differs from what this frame
  last put there, it is pushed as a new entry and pasted.
- With several selections, an entry with as many pieces gives one to each;
  otherwise every selection gets the pieces joined by newlines.

What is missing: `M-y`; reading the clipboard at any moment but a yank;
K5 and K6; any limit on the size of an entry.

## 4. The ring

```python
ed.kill_ring.entries  # oldest first; entries[-1] is the top
ed.kill_ring.push(pieces, frame=None)
ed.kill_ring.take(entry)  # K5: move to the top, set the clipboard
ed.kill_ring.remove(entry)
```

- **Size.** The `kill_ring_size` option, 60. The oldest entry goes when a
  new one would make more.
- **Duplicates (K6).** `push` compares the new entry's pieces with every
  entry's. A match is moved to the top, and nothing is added.
- **Large entries.** An entry of more than `kill_ring_entry_limit`
  characters (1,000,000) that came from the system clipboard is pasted by
  the yank that found it and not kept. One cut inside Neomacs is always
  kept: the user would otherwise lose text they just removed.
- **Empty entries** are not added (as now).
- An entry records where it came from, `"kill"`, `"copy"` or `"system"`,
  and when. The chooser shows neither at first; they are there for it to.

## 5. The system clipboard

### 5.1 Out

Unchanged: `push` and `take` send `clipboard.set` to the invoking frame's
frontend, with the pieces joined by newlines. The frame remembers the text
as the last it put there.

### 5.2 In (K2)

`frame.capture_clipboard()` awaits `clipboard.get`. If the reply is text,
is not empty, and is not what this frame last put there or last saw, it is
pushed with the source `"system"`. It is called:

1. **By `yank`,** before pasting, as now.
2. **When the window regains focus.** `input.focus(true)` starts it as a
   background task. Nothing waits on it, so a frontend slow to answer
   delays nothing.
3. **When the chooser opens,** before the list is built.

A failure or a timeout (0.5 s) is as if the clipboard were unchanged.

Consequences, stated so they are not surprises:

- Copy in a browser, switch back, kill a line, `M-y`: both are in the ring.
  Today the browser's text is lost, because the kill replaces it on the
  clipboard before any yank reads it.
- Copy A, copy B, switch back: only B is in the ring.
- The terminal frontend reports no focus changes, so there only cases 1 and
  3 apply.
- Text on the clipboard when the engine starts is captured at the first of
  these moments, like any other.

### 5.3 The X11 selection

Unchanged. The primary selection is not part of the ring: a middle click
pastes it directly (FRONTEND 13.1).

## 6. Yanking

- **`yank` (`C-y`, `s-v`, Edit > Paste).** Captures the clipboard (5.2),
  then inserts the top entry at every selection, replacing selected text.
  It remembers, per selection, the range it inserted, as markers.
- **`yank_from_ring` (`M-y`).** Opens the chooser (section 7). On a choice:
  - if the last command in this view was `yank` or `yank_from_ring` and its
    ranges are intact, the text in those ranges is replaced (K4);
  - otherwise the entry is inserted at every selection.

  Either way the entry is taken (K5), and the insertion is one undo step.
  "Intact" means nothing has edited the buffer since: the buffer's tick is
  compared.
- `C-g` in the chooser changes nothing, the ring's order included.

## 7. The chooser

### 7.1 What the user sees

```
┌─ Kill ring ───────────────┬───────────────────────────────┐
│ ▏                         │ def function_3(a, b):         │
├───────────────────────────┤     return a + b * 3          │
│ def function_3(a, b):  2↵ │                               │
│ return a + b * 3          │                               │
│ https://github.com/algo…  │                               │
│ value = 42 ⏎ other…  3 ✂ │                               │
└───────────────────────────┴───────────────────────────────┘
```

- **The list,** newest first. Each row is the entry's first line that is
  not blank, with leading space removed and cut to fit. A note at the right
  says how many lines there are when there is more than one, or how many
  pieces when the entry was made with several cursors.
- **The preview** is the whole of the highlighted entry, in a real view: it
  scrolls, wraps as the user's buffers do, and is highlighted in the mode of
  the buffer `M-y` was pressed in. The pieces of an entry with several are
  shown one after another with a rule between them.
- **Typing filters.** An entry stays if what was typed occurs anywhere in
  its text; case is ignored unless a capital was typed, as in search. The
  order stays newest first. The row shows the line the match is on.
- **Keys.** Up, down, `C-n`, `C-p` move; `M-y` again moves down, so the key
  can be held or tapped to walk the ring; Return chooses; `C-g` or Escape
  cancels. `C-d` removes the highlighted entry from the ring.
  `M-v` and `C-v` scroll the preview. A click chooses.
- With an empty ring, `M-y` says "The kill ring is empty" and opens nothing.

### 7.2 How it is built

The chooser is a minibuffer (ENGINE 7.5) with candidates, shown in a named
place, with one addition: a view to show beside it.

- **Engine.** `yank_from_ring` makes a minibuffer with a completer over the
  ring and places it in the place `"chooser"`. `minibuffer_show`'s options
  gain `preview`: the id of a view on a scratch buffer, `*kill ring*`, that
  the engine fills with the highlighted entry each time the selection
  moves. The minibuffer gets an `on_select` hook for that; it has none now.
- **A candidate carries its entry.** Candidates are matched to entries by
  identity, not by their text, since two entries can show the same row.
  `Candidate` gains a `value` field for this.
- **Electron** offers the place `"chooser"` when it attaches (as it offers
  `"search"`), and draws it as a box over the middle of the main area: the
  input and the list on the left, the preview view on the right. The list
  is the existing popup, anchored in the box instead of under the prompt.
- **A frontend without the place** gets the ordinary prompt with its popup,
  as `frame.place` already arranges. If it draws panels, as the terminal
  frontend does, the preview buffer is shown in a panel at the bottom for as
  long as the chooser is open, with the role `preview`, and is closed with
  it (K9). The keyboard stays in the prompt. A frontend that draws neither
  gets no preview.

  ```
  ┌──────────────────────────────────────────┐
  │ the buffer being edited                  │
  │                                          │
  ├─ *kill ring* ────────────────────────────┤
  │ def function_3(a, b):                    │
  │     return a + b * 3                     │
  ├──────────────────────────────────────────┤
  │ Yank: ▏                                  │
  │ def function_3(a, b):               2↵   │
  │ return a + b * 3                         │
  └──────────────────────────────────────────┘
  ```

Nothing about the chooser is particular to the kill ring. The same place
and `preview` option would serve a buffer switcher or a file finder with a
preview; none is proposed here.

### 7.3 Protocol changes

| Change | Kind |
|---|---|
| `minibuffer_show` options gain `preview: view` | Addition. A frontend that does not know it ignores it. |
| A new named place, `chooser` | Addition. Places are offered by the frontend at attach. |

No protocol version change is needed (STABILITY 5: additions a client can
ignore are compatible).

## 8. Implementation stages

Each ends in something that can be used.

**K-1: the ring.** `kill_ring_size`, `kill_ring_entry_limit`, duplicates
(K6), `take` and `remove`, sources. Capture on focus and at the chooser's
opening (5.2). *Runnable:* copy in another program, switch back, kill a
line, and find both in `ed.kill_ring.entries`.

**K-2: `M-y` with the prompt.** `yank_from_ring`, the completer, filtering,
replace-after-yank (K4), `Candidate.value`, `on_select`, the keys of 7.1,
and the preview in a bottom panel with the role `preview` (K9). Shown with
the ordinary prompt and popup in both frontends. *Runnable:* `M-y`, type a
few letters, watch the preview follow, Return, in the terminal frontend.

**K-3: the chooser box in Electron.** The place `"chooser"`, the `preview`
option and view, the box's layout and keys, the preview's highlighting.
*Runnable:* the picture in 7.1, in the Electron window.

## 9. Verification

- **Ring:** property test against a model list: size limit, duplicates
  move, `take` reorders, pieces survive.
- **Capture:** with the headless frontend answering `clipboard.get`: each
  of the three moments captures once; the same text is not captured twice;
  a kill made after a capture leaves both in the ring; a frontend that
  does not answer delays a yank by the timeout and a focus by nothing.
- **Yank:** with three cursors and entries of one, three and two pieces;
  `M-y` after `C-y` replaces at every cursor and undoes in one step; `M-y`
  after an edit inserts.
- **Chooser:** filtering by text on a later line; two entries with the
  same first line are told apart; `C-d` removes; `C-g` leaves the ring's
  order alone.
- **Electron:** in `npm run smoke`, with real keys: the box appears, the
  preview follows the selection, Return pastes and the system clipboard
  holds the chosen text.

## 10. Risks

| Risk | Consequence | Mitigation |
|---|---|---|
| The focus capture reads a secret that is on the clipboard when the user switches back | It is in the ring until 60 newer entries push it out, and shows in the chooser | It was copied to be pasted, usually here. `C-d` in the chooser removes it. Skipping what a password manager marks as concealed is possible on macOS and is not planned at first. |
| A very large clipboard | Memory, and a slow chooser | `kill_ring_entry_limit` (section 4). |
| Two frames, two clipboards (a remote frontend) | An entry captured from one is offered in the other | Intended: one ring (K7). |

## 11. As built

Everything above was built as described, with these differences and details.

- **Where things are.** The ring is `neomacs/clipboard.py`; the chooser is
  `neomacs/std/killring.py`; `yank` is in `neomacs/std/editing.py`. The
  commands are `yank`, `yank_from_ring` (`M-y`, and Edit > Paste from
  History), `kill_ring.remove`, `kill_ring.preview_down` and
  `kill_ring.preview_up`.
- **`ed.kill_ring.entries` holds `Entry` objects** (`pieces`, `source`,
  `time`, and `text` for the pieces joined), not lists of strings as before.
- **Clicking a row does not choose it.** Section 7.1 says a click chooses.
  The popup has never taken clicks, in any prompt; making it do so is a
  change to the popup and the protocol, and was left out.
- **A candidate that stands for something is chosen by where the highlight
  is.** A prompt that takes the highlighted candidate otherwise prefers
  what was typed when that is itself a candidate's text. Two entries can
  read the same, so that rule is not applied to candidates with a `value`.
- **The row for an entry of nothing but line breaks** shows a mark for each
  (`⏎`), up to eight.
- **The preview buffer** is `*kill ring*`. It has no line numbers, is never
  marked modified, and is left out of the recovery journal. It stays in the
  buffer list between uses.
- **In Electron** the chooser is the prompt's own box, made wider and given
  a second column: no new element kinds, and the popup is the same popup.
  The preview is an ordinary view, so it scrolls and wraps like any other.
- **Verified:** 14 tests through the reference frontend, covering every
  item of section 9 but the property test of the ring against a model
  (the ring's rules are tested by example instead). One check in
  `npm run smoke` with real keys in the Electron window: the list, the
  preview following the highlight with its syntax colours, Return pasting,
  and the system clipboard holding what was chosen. A picture of the page
  was saved and looked at.
- **Not verified:** the terminal frontend's preview panel was tested
  through the headless frontend, which lays out as the terminal does; it
  was not run in a real terminal. Capture on focus was tested by sending
  the focus message; it was not tried by switching between real windows.


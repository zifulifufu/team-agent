/**
 * Composer drafts, kept per place rather than inside the page that edits them.
 *
 * Why this exists: `App` renders the chat as `<ChatView key={view.gid} …>`, so opening another
 * group — or another page and coming back — **unmounts** the view. Anything it kept in its own
 * `useState` is thrown away with it, which is how a half-typed message disappeared. State that has
 * to outlive a remount cannot live in the component that gets remounted.
 *
 * Keyed per group, never one shared slot: text written for one group must not turn up in another
 * box. Putting a sentence meant for one project in front of another — and sending it — is a worse
 * failure than losing it, and it is the kind of mistake that only shows up in front of somebody.
 *
 * **Memory only, deliberately.** A draft is unfinished writing: putting it in `localStorage` (which
 * is Chromium's profile, not this app's data directory) would leave a second copy of the user's
 * words somewhere they never chose to keep them, and this app otherwise keeps everything under one
 * data directory they can see and delete. Reloading the app loses drafts; moving around in it
 * does not. If that turns out to be the wrong trade, it is one place to change.
 */

import type { Attachment } from "./api";

/** The home screen's composer belongs to no group; group ids are hex, so this cannot collide. */
export const HOME_DRAFT = "home";

const texts = new Map<string, string>();
const files = new Map<string, Attachment[]>();

/** What was typed for `key` ("" when there is nothing). */
export const draftText = (key: string): string => texts.get(key) ?? "";

export function setDraftText(key: string, value: string): void {
  if (value) texts.set(key, value);
  else texts.delete(key);            // empty means "nothing to remember", not "remember nothing"
}

/** Files attached but not yet sent. They are already uploaded, so dropping them would also leave
 *  an orphaned upload and a user who cannot tell whether the attachment went anywhere. */
export const draftFiles = (key: string): Attachment[] => files.get(key) ?? [];

export function setDraftFiles(key: string, value: Attachment[]): void {
  if (value.length) files.set(key, value);
  else files.delete(key);
}

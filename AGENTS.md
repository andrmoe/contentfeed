# Agent workflow for this repo

This checkout runs on a VM whose GitHub credentials belong to the `andrmoe-bot` account.
`origin` (`github.com/andrmoe/contentfeed`) is read-only for that account; `bot-fork`
(`github.com/andrmoe-bot/contentfeed`) is where agent branches are pushed. Changes reach
upstream only through pull requests from the fork, such as "Merge pull request #69 from
andrmoe-bot/feat/…" in the history.

## Standard workflow

1. Branch off `main`: `git switch -c feat/<name>` (or `fix/<name>`).
2. Run the tests before pushing: `python3 -m unittest` and `python3 -m py_compile *.py`.
   Don't edit `VERSION` for a change: the version in the page footer is its major.minor plus the
   commit count, so it counts up by itself. Bump the minor number only for a notable release.
3. Commit, then push the branch to the fork (this VM's credentials are used automatically;
   `gh` is authenticated as andrmoe-bot and git uses it as the HTTPS credential helper):

       git push bot-fork <branch>

4. Open the pull request into upstream:

       gh pr create --repo andrmoe/contentfeed --head andrmoe-bot:<branch> --base main \
         --title "<short title>" --body "<what and why>"

5. Verify with `gh pr view <pr-url>`.

Never push to `origin` directly and never force-push; the bot account cannot write to
upstream, and PRs are reviewed there.

## Credit

Agents credit themselves on everything they write, so the history shows which work was done by an
agent and by which model:

- Every commit, merge commits included (don't use `git merge --no-edit`, which drops it), ends with
  a trailer naming the agent and its model, such as:

      Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>

- Every pull request description ends with a line naming the tool that generated it, such as
  `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.

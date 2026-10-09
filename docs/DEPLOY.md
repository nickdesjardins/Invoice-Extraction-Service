# Putting the service online, step by step

This guide puts the project online for free, in two places:

| What | Where | Address |
|---|---|---|
| The **project page**: a small page that is there at once. It shows two recorded runs and the measured figures, and it leads to the live demo | GitHub Pages | <https://nickdesjardins.github.io/Invoice-Extraction-Service/> |
| The **live demo**: the service itself, with its page at `/` | Render (a free web service) | `https://<service name>.onrender.com/` |

The project page is the address to give to people. The live demo sleeps when nobody
has used it for 15 minutes, and it needs about a minute to wake up. Once Part 3 of
this guide is done, the project page starts waking it as soon as the page has loaded,
and one line of the page says how far that is.

**What it costs:** nothing. GitHub Pages is free for a public repository, and so is
GitHub Actions, the part of GitHub that runs the workflows described below. Render's
documentation says that its free web service needs no payment. And the service, set up
as this guide says, only ever calls free models.

**What you need:** the repository on GitHub, an OpenRouter API key, and about half an
hour of your time. Part 4 adds a wait of a quarter of an hour.

- The repository has to be public: the word **Public** stands beside its name on
  github.com.
- The key is the one in your `.env` file. Where a step says "paste the key", open that
  file in an editor and copy the key from there.
- To see that the key works, use the command in
  [USAGE.md, "Check your key and allowance"](USAGE.md#check-your-key-and-allowance).
  Run it in the project folder, and write `.\.venv\Scripts\python.exe` where the
  command says `python`. The command reads the key from your `.env` file, so the key is
  never typed.

**How far this guide has been tried.** It was written from the documentation of GitHub
and of Render as it read on 8 and 9 October 2026, and from this repository's own files.
Nobody has walked through it on the real sites yet. Where a button has another name on
your screen, go by the screen. What the repository itself decides is checked by a test
(`tests/test_docs.py`): the names of the workflows and of their jobs, the line in
`site/index.html`, and what `render.yaml` sets.

**Good to know before you start**

- Three workflows run on GitHub once the code is pushed. A workflow is a list of
  steps that GitHub runs for you. Their names appear in the repository's **Actions**
  tab: **Tests** (the test suite), **Model check** (tests the free models about once a
  week and publishes the ones that work) and **Project page** (checks the project page
  and publishes it). The steps of a workflow are grouped into jobs. Each time GitHub
  carries a workflow out, that is one run.
- The address of the project page is exact about capital letters. Give it to people
  as a link; do not ask them to type it.
- Never paste the API key into a command line. A shell keeps its history in a file on
  disk, and the key would stay there in plain text. In this guide the key is pasted
  into two web forms, and nowhere else. If a key was ever typed into a command line,
  make a new one at <https://openrouter.ai/keys>, delete the old one there, and write
  the new one into `.env`.
- Nothing here needs the service to run on your own PC. If it runs there as well,
  start one server only, and leave the line `OPENROUTER_MODELS=` in `.env` empty
  ([USAGE.md, section 9](USAGE.md#9-choosing-and-changing-models)).

---

## Part 1: GitHub (about 10 minutes)

Do steps 1 to 3 **before** pushing. Then the first run of each workflow can succeed,
and GitHub tells you when one does not.

### 1. Switch GitHub Pages on

1. Open the repository on github.com.
2. Click **Settings**: the last tab in the row under the repository's name, the row
   that begins with **Code**. These are the settings of the repository, not the ones
   behind your picture.
3. In the left sidebar, click **Pages**.
4. Under **Build and deployment**, set **Source** to **GitHub Actions**.

**What you should see:** the box under **Source** reads **GitHub Actions**. There is no
Save button: the choice is kept as soon as it is made. Under the box, GitHub suggests
workflows of its own. Choose none of them: the repository brings its own workflow, and
step 4 pushes it.

### 2. Give the scheduled test its key

The repository tests the free models on a schedule and publishes which ones work,
so that a service that slept for weeks wakes up on models that are still there.
That test needs an OpenRouter key, stored as a *secret*. (GitHub hides a secret from
everyone, in logs too. A *variable* can be read, so a key must never be one.)

A second key made on openrouter.ai only for this job is a good idea: it can then be
withdrawn on its own. Make it before you go on, at <https://openrouter.ai/keys>, and copy
it when it is shown. Both keys draw on the same daily allowance, because OpenRouter
counts per account.

1. On github.com again, in the repository's **Settings**: in the left sidebar under
   **Security**, click **Secrets and variables**, then **Actions**.
2. On the **Secrets** tab, click **New repository secret**.
3. **Name:** `OPENROUTER_API_KEY` (exactly this). **Secret:** paste the key: the second
   key if you made one, and the key in your `.env` file if not. Click **Add secret**.
4. Look at the **Variables** tab. If the key, or any other setting of the service,
   is listed there, delete it: nothing in this repository reads variables, and a key
   stored there is visible.

**What you should see:** on the **Secrets** tab, one row named `OPENROUTER_API_KEY`.
Its value is never shown again. It can only be replaced.

Without the secret nothing breaks. The job then says what is due, in a yellow warning
on the run, and tests nothing. Every service starts from the built-in default chain.

### 3. Switch GitHub's emails for failed runs on

1. On github.com, click your picture at the top right, then **Settings**.
2. In the left sidebar, click **Notifications**.
3. Scroll down to the heading **System**, and find **Actions** under it. Open the menu
   beside it. While nothing is switched on, the menu reads **Don't notify**.
4. In the menu, **Email** and **Only notify for failed workflows** both have to be
   selected. Select the ones that are not, then click **Save**.

**What you should see:** the menu does not read **Don't notify**. From now on a run
that fails sends you an email. A red **Model check** run is how this project tells you
that it needs you.

### 4. Push

In a terminal, in the project folder, run these three commands first:

```powershell
git status
git add -A
git status
```

On Windows, `git add -A` can print a warning for every file. Each one says
`LF will be replaced by CRLF`. The warning is about line endings on your PC, and nothing
that is pushed changes because of it.

Look at what the second `git status` lists before you go on:

- The files `.env` and `model_cache.json` must not be in the list. Both are ignored on
  purpose: the first holds your key, and the second is the service's own note of the
  chain it chose.
- Nothing may be left under "Untracked files": a file that is listed there is not
  pushed. The workflows and the host need more than the code in `app/`. They read
  `.python-version`, `render.yaml`, `pytest.ini` and `requirements.txt`, and the
  folders `.github/workflows/`, `benchmarks/`, `site/` and `tests/`.

When the list is right, commit and push:

```powershell
git commit -m "Model upkeep, call ceiling, project page, deployment files"
git push
```

**What you should see:** `git push` ends with a line like `main -> main`, and the files
appear in the repository on github.com.

### 5. Watch the first runs

Click the **Actions** tab of the repository. The push has started one run for each of
the three workflows. All three runs carry your commit message as their title, and the
name of the workflow stands under the title. A click on a workflow's name in the list
on the left shows the runs of that workflow only. A run shows a yellow dot while it
works. Then it shows a green tick or a red cross. Click a run to see its jobs.

- **Tests**: one job, `tests`, with a green tick. Its step "Run the test suite" ends
  with one line that gives the number of tests that passed, and `1 skipped`. The
  skipped test waits for the address of the live demo, which Part 3 fills in. Some
  tests run a page's script with Node.js, a program that runs JavaScript outside a
  browser. GitHub's machines have Node.js, so those tests run there and are not
  skipped.
- **Project page**: two jobs with a green tick each, "Does the page say what the
  records say?" and "Publish site/".
  If "Publish site/" is red and says "Get Pages site failed", Pages was not on yet:
  do step 1, then open the menu **Re-run jobs** at the top right of the run and click
  **Re-run failed jobs**.
- **Model check**: the push itself starts the first test of the free models. It spends
  about 23 free requests when OpenRouter lists the same four models as on 7 October
  2026. Expect about four minutes: the requests start six seconds apart, and GitHub
  needs some time to set the job up. It has three jobs: "Test the free models, if a
  test is due", "Publish the tested chain" and "Is a tested chain published?". What
  you see tells you how it went:

  | The three jobs | What it means |
  |---|---|
  | The first two have a green tick, and the third is greyed out as **skipped** | The good outcome: the models were tested, and the tested chain was published. The third job only runs when you have something to do |
  | The first has a green tick, the other two are skipped, and the run shows a yellow warning that says "this job has no key" | The secret of step 2 is missing or has another name. Nothing was tested and nothing was published. Add the secret, then start the workflow by hand. In the **Actions** tab, click **Model check** in the list on the left, then **Run workflow**, and **Run workflow** once more in the small panel that opens. Leave the box in that panel unticked: a test is due anyway |
  | The first has a green tick, the other two are skipped, and the summary under the run says "No test today" | No test was due. That happens when a tested chain is already published and still fresh, for example after a second push. Nothing was spent, and there is nothing to do |
  | The third has a red cross | You have something to do. Open the first job and read the end of its step "Decide, test, and write the file to publish". A line that begins `Failing this run on purpose:` says what it is. The [README, "The scheduled test"](../README.md#the-scheduled-test), lists the five reasons |

  After a good run, do not start another one by hand with the box "Test the free
  models now, even if no test is due" ticked. It would repeat the whole test and spend
  the same free requests again.

### 6. Look at what was published

1. Open the project page: <https://nickdesjardins.github.io/Invoice-Extraction-Service/>.
   A new page can take up to ten minutes to appear.

   **What you should see:** the measured figures, two recorded runs, and under the
   button **Code on GitHub** the sentence "The live demo is not online yet. This page
   shows recorded runs instead." That is right: Part 2 creates the live demo.
2. Look at the tested chain. On the repository's front page, open the branch menu (it
   says **main**) and choose **tested-models**. If the menu has no **tested-models**, the
   run published nothing: go back to the table in step 5.

   **What you should see:** two files, `README.md` and `chain.json`. The job created
   this branch. Do not create it by hand, and do not edit the file: the next run
   replaces it. Services read the file at
   `https://raw.githubusercontent.com/nickdesjardins/Invoice-Extraction-Service/tested-models/chain.json`.
   A new file can take up to five minutes to appear at that address.

   Click `chain.json` to read it. Near its top, the list `"chain"` names the tested
   chain. It can name three models where the default chain has two. That is right. A
   chain has three places, and a test also fills the spare third one: with a free model
   that passed, or with one that was right but slow. On 7 October 2026
   `dots-studio/dots-3-note-preview:free` was right but slow. A third model is asked
   only when the two before it gave no invoice.

   If the service also runs on your own PC, with the line `OPENROUTER_MODELS=` empty,
   restart it after those five minutes. Its log should then hold a line that begins
   `Using the chain published by the scheduled test of`.
3. On the repository's front page, click the gear beside **About**, tick
   **Use your GitHub Pages website**, and click **Save changes**.

   **What you should see:** the address of the project page under **About**. Someone
   who starts on GitHub then finds the page.

---

## Part 2: Render (about 10 minutes)

### 1. Create an account

1. Go to <https://dashboard.render.com/register>.
2. Choose **GitHub** to sign up with your GitHub account, and approve.

**What you should see:** the Render Dashboard, with no service in it yet. If Render
asks what you are building, any answer is fine. Do not add a payment card: without
one, nothing can be billed.

### 2. Create the service from the repository's own settings file

The repository holds a file, `render.yaml`, that tells Render everything about the
service. Render calls such a file a *Blueprint*. So nothing has to be typed into a
form except the key.

1. In the Render Dashboard, click **New**, then **Blueprint**.
2. Render shows your repositories. The first time, it asks to connect your GitHub
   account: choose **Only select repositories**, pick this repository, and click
   **Install**.
3. Click **Connect** next to the repository.
4. **Blueprint Name:** anything, for example `invoice-extraction`. **Branch:** `main`.
5. Render lists what it will create: one web service, `invoice-extraction-service`,
   on the **Free** plan. It asks for one value, `OPENROUTER_API_KEY`: paste the key of
   the service, the one in your `.env` file. Type nothing else.
6. Click **Deploy Blueprint**.

**What you should see:** Render starts the first deploy of a service named
`invoice-extraction-service`. A deploy is Render's word for building one version of the
service and starting it.

The form asks for nothing else. Three things are left alone on purpose. Later you can
find them on the service's **Environment** and **Settings** pages, and they are left
alone there too:

- **Do not add `OPENROUTER_MODELS`.** Leaving it out is what lets the service look
  after its own models.
- **Leave the health check path empty.** With no path, Render only checks that the
  service accepts a connection. The service has a route `/health`, but it answers 503
  while no key is set. With that route as the health check, Render would stop sending
  visitors to the service and restart it again and again. The service's own page, which
  explains the missing key, would never be seen.
- **Leave the start command as the file gives it.** It starts one process, and what
  the service remembers lives in the memory of that one process.

This is what `render.yaml` sets, so that you do not have to:

| In `render.yaml` | What it does |
|---|---|
| `plan: free` | A free web service. Without this line Render creates a paid one |
| `region: virginia` | Where the service runs. It cannot be changed once the service exists |
| `buildCommand: pip install -r requirements.txt` | Installs the packages, in the versions the tests passed on |
| `startCommand: uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 1` | Starts the service as one process |
| `ENVIRONMENT` is `production` | Hides `/docs` and the page's button "Run the test suite", and makes the page's own routes refuse callers that are not a browser |
| `OPENROUTER_API_KEY` with `sync: false` | Render asks for the key once, in the form. The key is never in the file |
| `CORS_ALLOW_ORIGINS` | Filled in by Render with the service's own address |
| `ignoredPaths` | A push that changes only `site/`, `docs/`, `README.md`, `.github/` or `tests/` does not rebuild the service |

### 3. Watch the first deploy

Click the service's name, then **Deploys**, then the deploy at the top of the list: the
page shows its log as it happens. The first deploy takes a few minutes: Render installs
the Python packages, then starts the service.

**What you should see:** the deploy's status turns to **Live**, and the log holds lines
like these, among others. The first and the last are Render's. The three between them
are the service's own: in the real log each of them starts with the date and the time,
and the date of the test is another one.

```text
==> Running 'uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 1'
INFO [app.model_chooser] Using the chain published by the scheduled test of 2026-10-12T06:21:47Z: ['nvidia/nemotron-3-super-120b-a12b:free', 'liquid/lfm-2.5-2.6b:free'] (https://github.com/.../actions/runs/...).
INFO [app.main] invoice-extraction-service v1.0.0 starting (environment=production, chain=['nvidia/nemotron-3-super-120b-a12b:free', 'liquid/lfm-2.5-2.6b:free'], timeout=30.0s)
INFO [app.model_chooser] Model choice: automatic. Using ['nvidia/nemotron-3-super-120b-a12b:free', 'liquid/lfm-2.5-2.6b:free'], the chain this project's scheduled test published (tested 2026-10-12T06:21:47Z).
==> Your service is live
```

The two lines of `app.model_chooser` say that the service read the chain that Part 1
published. The lists in this sample hold the two default models. In your log they hold
the models of `chain.json`, which you read in Part 1, step 6. When that chain is not the
default chain, for example because it names a third model, one more line of
`app.model_chooser` stands above the first. It begins `Model chain in use is now`, and
it says that the service went from the default chain to the published one.

If nothing was published, the two lines read differently, and that is no fault of
this step. The first then says that the published list `was not used`, and the second
says `Using the tested default`.

One line must not be there: `OPENROUTER_API_KEY is not set`. It means that the key was
not entered in step 2 (see "When something goes wrong" below).

### 4. Find the address and open it

The address is at the top of the service's page: `https://<service name>.onrender.com`.
(If the name `invoice-extraction-service` was already taken on Render, a few letters
are added to it.) Note it down: Part 3 needs it.

Open it. **What you should see** is the service's own page, with:

- the status at the top right saying **Operational**;
- under the model chain, a line saying where the chain comes from, for example
  "From this project's scheduled test of ...";
- at the very bottom, the service's name and version followed by **production**;
- no "API docs" link and no "Run the test suite" button (both are for development).

---

## Part 3: Tell the project page where the live demo is (5 minutes)

The project page keeps the live demo's address in exactly one place: line 103 of
`site/index.html`. To find it, search the file for `demo-link`.

1. In that line, replace `live-demo-not-deployed-yet.invalid` with the name in the
   address you noted in Part 2: the part after `https://`, in lower case, for example
   `invoice-extraction-service.onrender.com`. Leave `https://` in front of it and
   `/#playground` behind it. Change nothing else.
2. Check it. In a terminal, in the project folder:

   ```powershell
   .\.venv\Scripts\python.exe -m pytest -q tests/test_front_door.py
   ```

   **What you should see:** every test passes, and none is skipped any more. (On a PC
   without Node.js two tests are skipped, and that is fine.) If the address is
   mistyped, a test fails and says what is wrong with it.
3. Commit and push:

   ```powershell
   git add -A
   git commit -m "Project page: the live demo's address"
   git push
   ```

**What you should see:** in the **Actions** tab, a new run of **Tests** and one of
**Project page**, both with a green tick. **Model check** does not start for this
push, because the push changes no file that the test of the models is made of. Render
does not redeploy either: the folder `site/` is on its list of paths to ignore.

GitHub publishes the page again within a minute or two. Browsers keep the old page
for about ten minutes.

---

## Part 4: Check that it works (10 minutes, after a wait of a quarter of an hour)

Read the list at the end of this part before you start. Two of the things to note
down can only be seen while you do the steps.

1. Close the live demo's page and the project page. Wait a quarter of an hour, so that
   the live demo is asleep. Then open the project page.

   **What you should see:** after a few seconds the line under the buttons begins with
   "Waking the live demo." and counts the minutes and seconds beside it. After about a
   minute it begins with "The live demo is awake." If it says so at once, the live demo
   was not asleep yet. Nothing is wrong: go on, or wait longer and open the project page
   again to see the wake-up.
2. Click **Open the live demo**.
3. On the live demo's page, click **Every model fails** in the list headed
   **Scenarios**, then press **Run extraction**. The scenario simulates its failures,
   so it calls no model and costs nothing.

   **What you should see:** the result "503 Every model failed".
4. Run **Invoice inside an email thread** in the same way. This one calls a real model.

   **What you should see:** the result "200 Invoice extracted", usually within a
   quarter of a minute. When the default models were measured, the first one took
   about 7 seconds for its usual answer and about 14 seconds for its slowest.
5. Scroll to **What happens when a free model disappears** and press **Replay this
   test**.

   **What you should see:** the test on show is played again, call by call. Nothing
   is called for that.

Some things could not be measured before the service was really online. Note them
down the first time:

- how long the wake-up took: the last count on the project page before its line says
  that the live demo is awake. The count is gone after that;
- whether the steps of a run on the live demo's page appear one by one, or all at
  the end. If they come all at the end, the page hears nothing while a run is under
  way, and it stops waiting for a run after 120 seconds in which nothing arrived. The
  slowest run takes about 90 seconds;
- in Render's **Logs**, a line of the form "OpenRouter counts N of M free requests
  used today". If the log never shows that line, and says instead that the count
  "could not be read", the service does not know what is left of the day. Visitors then
  get 20 live model calls for each start of the service, and no more;
- whether Render's **Logs** hold a line that begins "Could not save the chosen model
  chain". If they do, it is no fault: the host does not let the service write a file,
  and the service goes on with the chain from its memory;
- whether Render's **Logs** ever hold a `WARNING` that says "did not finish" or "could
  not be read in time". Either means that OpenRouter took longer to answer the host
  than the service waits: 4 seconds for the count of the day, and 5 seconds for its
  pages about the models of the chain.

---

## Day to day

- **Changing the code:** push to `main`. Render builds and deploys by itself, and
  keeps the old version running until the new one is up. A push that changes only
  `site/`, `docs/`, `README.md`, the workflows or the tests does not redeploy the
  service.
- **Looking at what the service did:** Render Dashboard, the service, **Logs**.
  [USAGE.md, section 14](USAGE.md#14-reading-the-logs), says what each line means.
- **The service sleeps.** After 15 minutes without a request Render stops it; the
  next request starts it again, which takes about a minute. Everything it had in
  memory is gone at that point, and so is every file it wrote. This is what the
  free plan is; the project page and the published list of tested models exist
  because of it.
- **Limits of the free plan:** 750 hours of running time a month for the whole
  account (a sleeping service uses none) and a monthly amount of build time. Without
  a payment card nothing can be billed. When a limit is reached, Render stops the
  service, or stops building new versions, until the next month. Usage is on the
  **Billing** page.
- **When GitHub pauses the scheduled test.** In a public repository, scheduled
  workflows stop after 60 days without activity in the repository. The sign: from day
  14 on, the service's log and the live demo's page say how old the published list is.
  To bring the test back: **Actions** tab, **Model check**, **Enable workflow**. A push
  does not start a workflow that is paused.
- **When a Model check run is red:** you have something to do. Open the run, then its
  first job, and read the end of the step "Decide, test, and write the file to publish".
  A line that begins `Failing this run on purpose:` says which of five reasons it is: no
  tested chain younger than three weeks is published, the key was refused, the job has
  no key and the default chain or the list is out of date, OpenRouter offers no model of
  the chain any more, or a model of the chain failed the test. The
  [README, "The scheduled test"](../README.md#the-scheduled-test), says what each of
  them means. A red run without such a line means that the job itself broke.
- **When two working models should change places,** or a model has to be replaced by
  one of your choice: that stays your decision, and it is made by measuring. The steps
  are in the [README, "When a model disappears"](../README.md#when-a-model-disappears).
- **Changing the key:** Render Dashboard, the service, **Environment**, change
  `OPENROUTER_API_KEY`, then **Save and deploy**. If the GitHub job uses the same key,
  replace its secret too (Part 1, step 2).
- **Renaming the repository or your GitHub account:** three addresses hold the old
  name. One is the address that services read the published list from,
  `DEFAULT_PUBLISHED_URL` in `app/config.py`. Change it at once. The old address keeps
  answering after a rename, so nothing looks wrong, until somebody else takes the old
  name: from then on it can answer with a file of theirs. Until the address is changed,
  every run of **Model check** shows a yellow warning that begins "This job publishes
  to". The other two are the address of the project page and the address of the
  repository itself. Search the repository for the old name and change it everywhere,
  in one commit. It is in `app/config.py`, in `site/index.html`, in
  `.github/workflows/pages.yml`, in `tests/test_front_door.py`, in
  `tests/test_published_chain.py` and in the documents. Then give the new address of
  the project page out.
- **Taking it offline:** Render Dashboard, the service, **Settings**: at the bottom
  the service can be suspended (which can be undone) or deleted.

## When something goes wrong

| What you see | What it means | What to do |
|---|---|---|
| The deploy fails during the build | A package could not be installed | Open the deploy's log and read the last lines; then **Manual Deploy**, **Clear build cache & deploy** |
| The deploy never becomes **Live** | The start command did not start the service | Check that `render.yaml` was not changed: its `startCommand` must stay as it is |
| The live demo's page says **No API key** | The key was not entered, or was entered empty | **Environment**, add `OPENROUTER_API_KEY`, **Save and deploy** |
| The project page says "The live demo has not answered for three minutes" | The service is suspended, the deploy failed, or the address in `site/index.html` is wrong | Open the live demo's address directly; then look at **Events** on Render |
| A red cross on **Project page** at the first push | Pages was not switched on yet | Do Part 1, step 1. Then open the menu **Re-run jobs** on that run and click **Re-run failed jobs** |
| A red cross on **Tests** | A test failed on GitHub's machine | Click the run, then the job `tests`. The end of the step "Run the test suite" names each test that failed. A PC without Node.js skips the 42 tests that run a page's script. On a PC that has Node.js, the same command shows every test that failed: `.\.venv\Scripts\python.exe -m pytest -q` |
| **Model check** shows a yellow warning "this job has no key" | The secret is missing or has another name | Part 1, step 2 |
| The live demo's address shows a loading page of Render's for about a minute | The service was asleep, and Render is starting it | Wait. This is why the project page is the address to give out |
| The live demo's page says "The demo is busy" or "Today's share of live runs is used up", with the status 503 | The service's own ceiling on model calls was reached | Wait for the time it names. Scenarios that only simulate failures still work |
| The live demo's page says **Models withdrawn** at the top right | OpenRouter no longer offers any model of the chain. The service looks for a replacement by itself, after a run that fails | Read the sentence under the model chain on that page: it says what the service has done about it so far. Then read the service's log ([USAGE.md, section 14](USAGE.md#14-reading-the-logs)) |
| A notice on the live demo's page ends with "Please contact the admin." | The page names no setting, because visitors read it. What happened is in the log, written for you | Render Dashboard, the service, **Logs**: read the last `WARNING` and `ERROR` lines of `app.model_chooser`. They say what is wrong, when the service tries again by itself, and what you can do ([USAGE.md, section 15](USAGE.md#15-troubleshooting)) |

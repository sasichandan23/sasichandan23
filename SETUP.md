# verdant — setup

Everything about this profile comes from `config.yaml`.
The generator holds no personal data.

```
config.yaml  →  scripts/generate.py  →  output/*.svg  →  README.md
```

---

## Publishing (do these in order)

**1. Create the repository on GitHub.**
New repository → name it **exactly** `sasichandan23` → **Public** →
do **not** tick "Add a README", `.gitignore`, or a license.
GitHub will show a note about a special repository — that confirms
the name is right.

**2. Push this folder.**

```bash
git init -b main
```

```bash
git add -A
```

```bash
git commit -m "verdant"
```

```bash
git remote add origin https://github.com/sasichandan23/sasichandan23.git
```

```bash
git push -u origin main
```

**3. Allow the scheduled job to commit.**
Settings → Actions → General → Workflow permissions →
**Read and write permissions** → Save.
Without this the regeneration runs but cannot push its result.

**4. Check the Actions tab.**
A run named "regenerate profile" should appear and go green. That run
rebuilds the SVGs on GitHub, which is what keeps the profile live.

---

## Adding the portrait

Drop any image into `assets/portrait/` — `.png`, `.jpg` or `.webp`,
highest quality you have. The newest file there wins, so swapping it
later is a file drop. Portrait-orientation images of at least
440 × 600 look sharpest; the frame centre-crops to fit.

Then either commit and push it (the Action rebuilds automatically),
or rebuild locally first:

```bash
python scripts/generate.py
```

Local rebuilds need Python with `pyyaml` and `pillow`:

```bash
pip install pyyaml pillow
```

---

## Projects

Right now the project cards are written by hand in `config.yaml`,
because none of the repositories carry a description.

To make the panel maintain itself, add a **description** and a few
**topics** to each repo on GitHub, then set:

```yaml
data:
  auto_projects: true
```

From then on the explorer builds from real repositories every six
hours — forks dropped, ranked by stars then recency, top 8 shown,
with each repo's own description, language, topics and star count.
New repositories appear on their own.

Live stats (repository count, stars, followers) are already live
either way.

---

## Changing anything else

Every visible string lives in `config.yaml`, including the panel
names under `labels:` — rename them and this becomes a different
operating system. The design system itself (layout, fonts, animation
timing, section order) stays fixed; that is what makes eight separate
images feel like one machine.

After any edit, rebuild and look before you push:

```bash
python scripts/generate.py
```

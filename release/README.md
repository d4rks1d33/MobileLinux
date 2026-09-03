# Releases

This directory holds the **ready-to-flash images** and release notes for each
supported device. On every push to `main`, a GitHub Actions workflow
([`.github/workflows/release.yml`](../.github/workflows/release.yml)) iterates
over the subdirectories here and publishes a **GitHub Release** for each device
whose metadata says it should be published.

```
release/
├── README.md               <- this file
└── <device>/               <- one per device (e.g. rhodep)
    ├── release.yaml        <- release metadata (version, tag, files, repo-only)
    ├── README.md           <- device-specific flashing guide (+ disclaimer)
    ├── CHANGELOG.md         <- what's new in this release
    ├── CHECKSUMS.sha256     <- sha256 of the images
    ├── rescue.img           \_ small boot images (~62 MB): normal git,
    ├── kali-boot.img        /  attached as GitHub Release assets
    └── kali-userdata.img.xz <- large system image: xz-compressed + Git LFS,
                                downloaded from the repo (NOT a release asset)
```

## Currently released devices

| Device | Model | Distro | Version | Tag |
|--------|-------|--------|---------|-----|
| [`rhodep/`](rhodep/) | Motorola Moto G82 5G | Kali + Phosh | 1.0.0 | `kali-rhodep-v1.0.0` |

## How the release workflow works

On push to `main`, for each `release/<device>/release.yaml` with
`publish: true`, the workflow:

1. reads the metadata (`tag`, `title`, `version`, `artifacts`,
   `repo_only_artifacts`, ...);
2. packages the device README + CHANGELOG + CHECKSUMS into a small `.zip`;
3. builds the release notes from `CHANGELOG.md`;
4. creates/updates a GitHub Release with tag `<tag>` (e.g. `kali-rhodep-v1.0.0`);
5. uploads the images listed in **`artifacts`** (`rescue.img`, `kali-boot.img`)
   plus the `.zip` as release **assets**.

If the tag already exists, the workflow updates it (idempotent), so re-pushing
does not create duplicates. Set `publish: false` to skip a device.

### Large images: compress with `xz` + Git LFS (important)

The userdata system image is large (the raw `kali-userdata.img` is ~8 GB for
rhodep). Two hard limits shape how we ship it:

- **GitHub Git LFS rejects any file larger than 2 GB.**
- Normal git and release assets are also impractical for multi-GB files.

So the large image is **compressed with `xz` and tracked with Git LFS**, which
brings ~8 GB down to ~1.6 GB — under the 2 GB LFS limit — and it is **not**
attached to the GitHub Release. Concretely:

- `rescue.img` and `kali-boot.img` (~62 MB each) → **normal git**, **attached**
  to the GitHub Release as assets.
- `kali-userdata.img.xz` (~1.6 GB) → **Git LFS** (see
  [`.gitattributes`](../.gitattributes)) and listed under
  `repo_only_artifacts:` in `release.yaml`. It is **not** a release asset;
  the release notes link users to download it from the repo
  (`release/<device>/kali-userdata.img.xz`, e.g. the `.../raw/main/...` URL, or
  via `git lfs`).

**Why `.xz` (and not raw)?** Besides fitting the 2 GB LFS limit, users flash it
without ever decompressing to disk — the flashing step pipes it through `xz`
(`xz -dc kali-userdata.img.xz | nc ...`), so they don't need ~8 GB of free space
on their PC. Keep the compressed image tracked by LFS; **never commit the raw
multi-GB `.img`** (it won't push, and it bloats history).

Working with LFS locally requires `git lfs install` once
(`apt install git-lfs`). Cloning the repo then fetches the compressed userdata
image into `release/<device>/`.

> If even `.xz` exceeds 2 GB for some device, either raise the compression
> (`xz -9`), split the image, or attach it manually to the Release via the web /
> `gh release upload` (release assets allow up to 2 GB each) and point
> `repo_only_artifacts` / the notes at that instead.

## Adding a new release

### New version of an existing device

1. Build the new images (see the repo's
   [build-and-flash guide](../docs/build-and-flash.md)):
   ```bash
   mobilelinux build <device> --distro kali \
       --input <known-good pmos-boot.img> --execute --allow-dangerous
   ```
2. Make sure Git LFS is set up once: `apt install git-lfs && git lfs install`.
3. Copy the small boot images from `out/<device>/` into `release/<device>/`:
   ```bash
   cp out/<device>/rescue.img out/<device>/kali-boot.img release/<device>/
   ```
4. **Compress the large system image with `xz`** (it must stay under Git LFS's
   2 GB limit; `.xz` is what ships, not the raw `.img`):
   ```bash
   xz -6 -T0 -c out/<device>/kali-userdata.img \
       > release/<device>/kali-userdata.img.xz
   # check it's < 2 GB; if not, use xz -9 (or split / manual upload)
   ls -lh release/<device>/kali-userdata.img.xz
   ```
   The `.gitattributes` rule (`release/**/*-userdata.img.xz filter=lfs ...`)
   sends it to LFS automatically. **Do not** copy the raw multi-GB `.img` into
   `release/` — it won't push and bloats history.
5. Update `release/<device>/release.yaml`: bump `version` and `tag`
   (e.g. `kali-<device>-v1.1.0`). Keep the `.xz` under `repo_only_artifacts:`
   (linked, not attached) and the boot images under `artifacts:` (attached).
6. Update `release/<device>/CHANGELOG.md` with the new entry at the top.
7. Regenerate checksums over what you ship (boot images + the `.xz`):
   ```bash
   cd release/<device> && sha256sum kali-boot.img rescue.img *.xz > CHECKSUMS.sha256
   ```
8. Commit and push to `main`. The workflow publishes the release (attaches the
   boot images + a docs zip; the notes link users to the `.xz` in the repo).

### A brand-new device

1. Add the device definition (`devices/<vendor>/<codename>/device.yaml`) and
   build its images (see the [porting guide](../docs/porting-guide.md)).
2. Create `release/<codename>/` with:
   - `release.yaml` (copy `rhodep/release.yaml` and edit device/version/tag/
     `artifacts`/`repo_only_artifacts`),
   - `README.md` (device-specific flashing guide — adapt `rhodep/README.md` to
     the device's install strategy),
   - `CHANGELOG.md`,
   - the boot images (`rescue.img`, `<distro>-boot.img`), the **xz-compressed**
     system image (`<distro>-userdata.img.xz`, via LFS — see the compression
     step above), and `CHECKSUMS.sha256`.
3. Add the device to the table above.
4. Commit and push to `main`.

## Naming convention

- **Tag / release name:** `<distro>-<device>-v<version>` — e.g.
  `kali-rhodep-v1.0.0`. This is what a user searches for to find images for
  their exact phone.
- **Image files:** `rescue.img`, `<distro>-boot.img` (release assets), and
  `<distro>-userdata.img.xz` (xz-compressed, Git LFS, repo-only).

## Safety

Every device README ends with a **disclaimer**: flashing is at your own risk,
the authors accept no liability for bricked devices or lost data. See the
repository [DISCLAIMER.md](../DISCLAIMER.md).

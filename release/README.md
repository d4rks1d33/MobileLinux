# Releases

This directory holds the **ready-to-flash images** and release notes for each
supported device. On every push to `main`, a GitHub Actions workflow
([`.github/workflows/release.yml`](../.github/workflows/release.yml)) iterates
over the subdirectories here and publishes a **GitHub Release** for each device
whose metadata says it should be published.

```
release/
├── README.md            <- this file
└── <device>/            <- one per device (e.g. rhodep)
    ├── release.yaml     <- release metadata (version, tag, files to attach)
    ├── README.md        <- device-specific flashing guide (+ disclaimer)
    ├── CHANGELOG.md      <- what's new in this release
    ├── CHECKSUMS.sha256  <- sha256 of the images
    ├── rescue.img        \
    ├── kali-boot.img      >- the flashable images (attached as release assets)
    └── kali-userdata.img /
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

### Large images (Git LFS, not attached to the release)

GitHub caps a single **release asset** and a single **normal git file** at
generous-but-finite sizes, and the Kali **userdata** image is several GB. So:

- `rescue.img` and `kali-boot.img` (~62 MB each) are tracked in **normal git**
  and **attached** to the GitHub Release.
- `kali-userdata.img` (several GB) is tracked with **Git LFS** (see
  [`.gitattributes`](../.gitattributes)) and listed under
  `repo_only_artifacts:` in `release.yaml`. It is **not** attached to the
  release; instead the release notes link users to download it from the repo
  (`release/<device>/kali-userdata.img`, e.g. the `.../raw/main/...` URL, or via
  `git lfs`).

Working with LFS locally requires `git lfs install` once
(`apt install git-lfs`). Cloning the repo then fetches the userdata image into
`release/<device>/`.

## Adding a new release

### New version of an existing device

1. Build the new images (see the repo's
   [build-and-flash guide](../docs/build-and-flash.md)):
   ```bash
   mobilelinux build <device> --distro kali \
       --input <known-good pmos-boot.img> --execute --allow-dangerous
   ```
2. Copy the flashable artifacts from `out/<device>/` into `release/<device>/`:
   `rescue.img`, `kali-boot.img`, `kali-userdata.img`. (Make sure `git lfs
   install` has been run once so the userdata image goes into LFS.)
3. Update `release/<device>/release.yaml`: bump `version` and `tag`
   (e.g. `kali-<device>-v1.1.0`). Keep large images under
   `repo_only_artifacts:` so they're linked, not attached.
4. Update `release/<device>/CHANGELOG.md` with the new entry at the top.
5. Regenerate checksums:
   `cd release/<device> && sha256sum *.img > CHECKSUMS.sha256`.
6. Commit and push to `main`. The workflow publishes the release.

### A brand-new device

1. Add the device definition (`devices/<vendor>/<codename>/device.yaml`) and
   build its images (see the [porting guide](../docs/porting-guide.md)).
2. Create `release/<codename>/` with:
   - `release.yaml` (copy `rhodep/release.yaml` and edit device/version/tag/
     artifacts),
   - `README.md` (device-specific flashing guide — adapt `rhodep/README.md` to
     the device's install strategy),
   - `CHANGELOG.md`,
   - the images and `CHECKSUMS.sha256`.
3. Add the device to the table above.
4. Commit and push to `main`.

## Naming convention

- **Tag / release name:** `<distro>-<device>-v<version>` — e.g.
  `kali-rhodep-v1.0.0`. This is what a user searches for to find images for
  their exact phone.
- **Image files:** `<distro>-boot.img`, `<distro>-userdata.img`, `rescue.img`.

## Safety

Every device README ends with a **disclaimer**: flashing is at your own risk,
the authors accept no liability for bricked devices or lost data. See the
repository [DISCLAIMER.md](../DISCLAIMER.md).

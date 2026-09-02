# Importing from postmarketOS

If a device already has support in postmarketOS, you can bootstrap a MobileLinux
device definition from its pmaports data instead of writing everything by hand.
The importer produces a **draft** `device.yaml`; it fills what pmOS encodes as
structured data and leaves the rest as `untested` for you to complete from
evidence.

See [porting.md](porting.md) for the full porter workflow and
[device-schema.md](device-schema.md) for the field reference. The five imported
reference devices (`oneplus/enchilada`, `google/sargo`, `samsung/m0`,
`pine64/pinephone`, `purism/librem5`) were all seeded this way.

---

## How pmOS represents a device

postmarketOS stores device support in `pmaports.git` as:

```
device/<tier>/device-<vendor>-<codename>/
    deviceinfo      # shell-sourceable key=value facts
    APKBUILD        # packaging metadata
```

plus a matching `linux-<vendor>-<codename>` kernel package. `<tier>` mirrors the
pmOS maturity categories (`main`, `community`, `testing`, ...).

The `deviceinfo` file is a flat set of `deviceinfo_<key>="value"` lines: identity
(`name`, `manufacturer`, `codename`, `arch`, `chassis`), the flash method and
boot geometry (`flash_method`, `header_version`, `flash_pagesize`,
`flash_offset_*`, `generate_bootimg`), the DTB (`dtb`, `append_dtb`), the kernel
command line (`kernel_cmdline`), and the rootfs sector size
(`rootfs_image_sector_size`).

Crucially, pmOS does **not** store the per-feature hardware matrix in
`deviceinfo`. "What works / doesn't work" lives on the **wiki as prose**, and
install quirks are similarly documented in wiki text rather than structured
fields. That prose is exactly what MobileLinux formalizes — so it cannot be
imported automatically.

## What the importer maps automatically

`src/mobilelinux/importers/pmaports.py` parses `deviceinfo`
(`parse_deviceinfo`) and produces a draft dict (`deviceinfo_to_schema`). It maps:

| pmOS `deviceinfo` | MobileLinux field | Notes |
|-------------------|-------------------|-------|
| `manufacturer` / codename prefix | `vendor` | lowercased; falls back to the codename prefix. |
| `codename` (last segment) | `id` | `oneplus-enchilada` → id `enchilada`. |
| `name` | `model` | |
| `codename` | `codename`, and `aliases: [codename]` | |
| `arch` | `architecture` | default `aarch64`. |
| `chassis` | `chassis` | default `handset`. |
| `flash_method` | `install.strategy` | via `_FLASH_TO_STRATEGY` (below). |
| `flash_method == fastboot` | `install.unlock_required` | true only for fastboot. |
| `header_version`, `flash_pagesize`, `flash_offset_*` | `boot.android_bootimg.*` | boot geometry. |
| `generate_bootimg` | `boot.method` | `android-bootimg` if `true`, else `uboot-extlinux`. |
| `kernel_cmdline` | `boot.android_bootimg.cmdline` | |
| `dtb`, `append_dtb` | `device_tree.dtb` (with `.dtb`), `device_tree.append_dtb` | |
| `rootfs_image_sector_size` | `storage.rootfs_sector_size` / `rootfs_layout` | `4096` → `gpt-in-partition`, else `plain`. |

It also seeds a `kernel` block (`type: mainline`, `version: unknown`,
`build.method: pmbootstrap`, `pmaports_pkg: linux-<codename>`), a default
`ota: { strategy: single-rootfs, rollback: false }`, a starter `tests` list, and
`sources.imported_from: pmaports`. If the boot method is not `android-bootimg`
and `header_version` was absent, the `android_bootimg` sub-block is dropped.

### The `_FLASH_TO_STRATEGY` mapping

```
fastboot          -> fastboot
heimdall-bootimg  -> heimdall
heimdall-isorec   -> heimdall-isorec
uuu               -> uuu
rkdeveloptool     -> custom
0xffff            -> custom
none              -> sdcard
mtkclient         -> mtkclient
```

Anything not in the table maps to `custom`. This is best-effort — the porter
must confirm the strategy against the real device (e.g. rhodep's hand-written
`rescue-dd` has no pmOS equivalent, and a device listed as `fastboot` may still
deny `fastboot flash userdata`).

## What the importer CANNOT map

- **The per-feature hardware matrix.** pmOS keeps this as wiki prose, so the
  importer seeds every hardware feature at `status: untested`. Concretely it
  writes `untested` for `display, touchscreen, gpu, storage, usb, wifi,
  bluetooth, audio, battery, charging` and nothing more — you add the rest and
  promote statuses from evidence.
- **Install quirks / exact steps.** `install.steps` is seeded with a single
  `message` action noting the import is incomplete; the real ordered steps are
  yours to write.
- **SoC details.** `soc.vendor` and `soc.family` come in as `unknown`.
- **Kernel version.** `version` comes in as `unknown`.

The draft's own `sources.notes` says so: *"AUTO-IMPORTED DRAFT. Hardware statuses
are 'untested' — fill them in from the wiki + real tests. Verify install.steps
and SoC."*

## Command usage

`import_command` takes a `source` that may be a path to a `deviceinfo` file **or**
a device directory (in which case it appends `/deviceinfo`):

```
# point at the device directory (deviceinfo is found inside it)
mobilelinux import path/to/pmaports/device/community/device-oneplus-enchilada

# or point directly at a deviceinfo file
mobilelinux import path/to/pmaports/device/community/device-oneplus-enchilada/deviceinfo
```

The draft is written to `devices/<vendor>/<id>/device.yaml`. The importer
**refuses to overwrite** an existing `device.yaml` (it errors out), so re-running
is safe. On success it prints the path and a warning that the draft is
incomplete.

If the path has no `deviceinfo`, it errors: *"deviceinfo not found ... pass a
path to a pmaports device directory or its deviceinfo file."*

## Follow-up workflow

The import gets you a validatable skeleton; finishing the port is manual and
evidence-driven:

1. **Fill SoC and kernel** — replace the `unknown` `soc.vendor`/`soc.family` and
   kernel `version`.
2. **Fill the hardware matrix from the wiki + real tests.** Promote each
   `untested` feature only with `evidence` and a `test:` module. Mark absent
   hardware `not-present` (so it's excluded from the support %) and unfixable
   features `unsupported`/`broken`. Never mark `supported` without a passing
   test. See the evidence rule in [porting.md](porting.md).
3. **Confirm the install strategy and complete `install.steps`.** Verify the
   mapped strategy against the device, record `storage.partitions[].writable_via`
   facts, and write the ordered steps.
4. **Set OTA capabilities** — the importer defaults to `single-rootfs`; switch to
   `ab` with `slots`/`rollback`/`bootloader_integration` if the device has A/B
   slots (as `oneplus-enchilada` and `google-sargo` do).
5. **Set provenance** — add `sources.postmarketos_wiki` and `sources.pmaports`.
6. **Validate:**

   ```
   mobilelinux validate <id>
   mobilelinux check <id>
   mobilelinux flash <id> --dry-run
   python ci/validate.py
   ```

Only once the hardware matrix is evidence-based and `install.steps` are real is
the device more than a draft. Raise `maturity` to match reality as tests pass.

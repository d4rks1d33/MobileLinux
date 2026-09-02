# Vulkan on the Adreno 619 through Mesa's Turnip driver (freedreno / FD619).
# environment.d only covers systemd user services; login shells (where a Vulkan
# tool is run by hand over ssh) read profile.d, so point the loader here too.
#
# Only set it if the ICD manifest is actually present, so a Mesa upgrade that
# renamed the file cannot leave every login shell pinned to a path that no
# longer exists (which would report zero Vulkan devices instead of falling back
# to normal discovery).
if [ -f /usr/share/vulkan/icd.d/freedreno_icd.json ]; then
	VK_DRIVER_FILES=/usr/share/vulkan/icd.d/freedreno_icd.json
	VK_ICD_FILENAMES=/usr/share/vulkan/icd.d/freedreno_icd.json
	export VK_DRIVER_FILES VK_ICD_FILENAMES
fi

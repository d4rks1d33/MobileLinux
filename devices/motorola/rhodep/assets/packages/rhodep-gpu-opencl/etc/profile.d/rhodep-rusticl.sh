# Enable Mesa's rusticl OpenCL driver for the Adreno 619 (freedreno / FD619).
# environment.d only covers systemd user services; login shells (where hashcat
# is run) read profile.d, so set it here too.
export RUSTICL_ENABLE=freedreno

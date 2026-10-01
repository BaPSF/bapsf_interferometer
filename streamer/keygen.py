#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 Oak Ridge National Laboratory and Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Manage Curve25519 keypairs for use with SealedEncryptionOperator."""

import argparse
import nacl.encoding
import nacl.public

p = argparse.ArgumentParser(
    description=__doc__, epilog="Copy ONLY the public key to shared/HPC systems for writing."
)
sub = p.add_subparsers(dest="cmd", required=True)

gen = sub.add_parser("generate", help="generate a new keypair")
gen.add_argument("public_key_file")
gen.add_argument("secret_key_file")

drv = sub.add_parser("derive", help="derive public key from an existing secret key")
drv.add_argument("secret_key_file")
drv.add_argument("public_key_file")

dump = sub.add_parser("dump", help="hexdump an existing secret key pair")
dump.add_argument("public_key_file")
dump.add_argument("secret_key_file")

a = p.parse_args()

if a.cmd == "generate":
    sk = nacl.public.PrivateKey.generate()
    open(a.secret_key_file, "wb").write(sk.encode())
else:
    sk = nacl.public.PrivateKey(open(a.secret_key_file, "rb").read())

pk = sk.public_key
if a.cmd in ["generate", "derive" ]:
    open(a.public_key_file, "wb").write(pk.encode())

print(f"Public key: {a.public_key_file}\n  hex: {pk.encode(nacl.encoding.HexEncoder).decode()}")
if a.cmd in ["generate", "dump"]:
    print(f"Secret key: {a.secret_key_file}\n  hex: {sk.encode(nacl.encoding.HexEncoder).decode()}")

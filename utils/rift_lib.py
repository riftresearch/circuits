# utils to create witness data and verify proofs in python
import asyncio
import tempfile
import hashlib
from functools import reduce, cache 
import os
import math
import json
from typing import Union, TypeVar, Any, Coroutine
from pathlib import Path
import shutil

from pydantic import BaseModel
from eth_abi.abi import encode as eth_abi_encode
import aiofiles


from utils.noir_lib import (
    create_solidity_proof,
    initialize_noir_project_folder,
    compile_project,
    create_witness,
    normalize_hex_str,
    pad_list,
    hex_string_to_byte_array,
    split_hex_into_31_byte_chunks,
    create_proof,
    build_raw_verification_key,
    extract_vk_as_fields,
    split_hex_into_31_byte_chunks_padded,
    verify_proof
)

T = TypeVar('T')
BB = "~/.nargo/backends/acvm-backend-barretenberg/backend_binary"
MAX_ENCODED_CHUNKS = 226
MAX_LIQUIDITY_PROVIDERS = 175
MAX_INNER_BLOCKS = 24
CONFIRMATION_BLOCK_DELTA = 5


class SolidityProofArtifact(BaseModel):
    proof: str 


class RecursiveProofArtifact(BaseModel):
    verification_key: list[str]
    proof: list[str]
    public_inputs: list[str]
    key_hash: str


class RecursiveSha256ProofArtifact(RecursiveProofArtifact):
    key_hash_index: int


class LiquidityProvider(BaseModel):
    amount: int
    btc_exchange_rate: int
    locking_script_hex: str


class Block(BaseModel):
    height: int
    version: int
    prev_block_hash: str
    merkle_root: str
    timestamp: int
    bits: int
    nonce: int
    txns: list[str]

class SubBlockArtifact(BaseModel):
    proof_artifact: RecursiveProofArtifact
    first_block: Block 
    last_block: Block

class BlockTreeArtifact(BaseModel):
    height: int
    proof_artifact: RecursiveProofArtifact
    first_block: Block
    last_block: Block



NULL_BLOCK = Block(
    height=0,
    version=0,
    prev_block_hash='0' * 64,
    merkle_root='0' * 64,
    timestamp=0,
    bits=0,
    nonce=0,
    txns=[]
)

def compute_block_hash(block: Block) -> str:
    """
    Computes the double SHA-256 hash of a block header.

    Args:
    block (Block): The block for which to compute the hash.

    Returns:
    str: The hexadecimal representation of the hash.
    """
    # Convert block header information into a single bytes object.
    # Note: Bitcoin serializes these values in little-endian format.
    header_hex = (
        block.version.to_bytes(4, 'little') +
        # Reverse to little-endian
        bytes.fromhex(block.prev_block_hash)[::-1] +
        # Reverse to little-endian
        bytes.fromhex(block.merkle_root)[::-1] +
        block.timestamp.to_bytes(4, 'little') +
        block.bits.to_bytes(4, 'little') +
        block.nonce.to_bytes(4, 'little')
    )

    # Perform double SHA-256 hashing.
    hash1 = hashlib.sha256(header_hex).digest()
    hash2 = hashlib.sha256(hash1).digest()

    # Bitcoin presents hashes in little-endian format, so we reverse before returning.
    return hash2[::-1].hex()


async def block_toml_encoder(block: Block) -> list[str]:
    return [
        f"bits={block.bits}",
        f"height={block.height}",
        f"merkle_root={json.dumps(hex_string_to_byte_array(block.merkle_root))}",
        f"nonce={block.nonce}",
        f"prev_block_hash={json.dumps(hex_string_to_byte_array(block.prev_block_hash))}",
        f"timestamp={block.timestamp}",
        f"version={block.version}",
    ]




async def create_lp_hash_verification_prover_toml(
    lp_reservation_data: list[LiquidityProvider],
    compilation_build_folder: str
):
    """
    #[recursive]
    fn main(
        lp_reservation_hash_encoded: pub [Field; 2],
        lp_reservation_data_encoded: pub [[Field; 4]; rift_lib::constants::MAX_LIQUIDITY_PROVIDERS],
        lp_count: pub u32
    ) {
    """
    # 4 Fields = 4 * 31 bytes = 124 bytes
    # 3 bytes32 = 3 * 32 bytes = 96 bytes
    # 124 - 96 = 28 bytes
    padded_lp_reservation_data_encoded = pad_list(
        list(map(lambda lp: split_hex_into_31_byte_chunks(
            eth_abi_encode(
                ["uint192", "uint64", "bytes32"],
                [lp.amount, lp.btc_exchange_rate, bytes.fromhex(
                    normalize_hex_str(lp.locking_script_hex))]
            ).hex()
        ), lp_reservation_data)),
        MAX_LIQUIDITY_PROVIDERS,
        ["0x0"] * 4
    )

    vault_hash_hex = "00"*32
    for i in range(len(lp_reservation_data)):
        vault_hash_hex = hashlib.sha256(
            eth_abi_encode(
                ["uint192", "uint64", "bytes32", "bytes32"],
                [
                    lp_reservation_data[i].amount,
                    lp_reservation_data[i].btc_exchange_rate,
                    bytes.fromhex(normalize_hex_str(
                        lp_reservation_data[i].locking_script_hex)),
                    bytes.fromhex(vault_hash_hex)
                ]
            )
        ).hexdigest()

    vault_hash_encoded = split_hex_into_31_byte_chunks(vault_hash_hex)

    prover_toml_string = "\n".join(
        [
            f"lp_reservation_hash_encoded={json.dumps(vault_hash_encoded)}",
            f"lp_reservation_data_encoded={json.dumps(padded_lp_reservation_data_encoded)}",
            f"lp_count={len(lp_reservation_data)}",
        ]
    )

    print("Creating witness...")
    await create_witness(prover_toml_string, compilation_build_folder)


async def create_payment_verification_prover_toml(
    txn_data_no_segwit_hex: str,
    lp_reservation_data: list[LiquidityProvider],
    lp_count: int,
    order_nonce_hex: str,
    expected_payout: int,
    compilation_build_folder: str
):
    """
    fn main(
        txn_data_encoded: pub [Field; constants::MAX_ENCODED_CHUNKS],
        lp_reservation_data_encoded: pub [[Field; 4]; constants::MAX_LIQUIDITY_PROVIDERS],
        order_nonce_encoded: pub [Field; 2],
        expected_payout: pub u64,
        txn_data: [u8; constants::MAX_TXN_BYTES]
    ) {
    """
    # 4 Fields = 4 * 31 bytes = 124 bytes
    # 3 bytes32 = 3 * 32 bytes = 96 bytes
    # 124 - 96 = 28 bytes
    padded_lp_reservation_data_encoded = pad_list(
        list(map(lambda lp: split_hex_into_31_byte_chunks(
            eth_abi_encode(
                ["uint192", "uint64", "bytes32"],
                [lp.amount, lp.btc_exchange_rate, bytes.fromhex(
                    normalize_hex_str(lp.locking_script_hex))]
            ).hex()
        ), lp_reservation_data)),
        MAX_LIQUIDITY_PROVIDERS,
        ["0x0"] * 4
    )

    txn_data_encoded = pad_list(split_hex_into_31_byte_chunks_padded(
        normalize_hex_str(txn_data_no_segwit_hex)), MAX_ENCODED_CHUNKS, "0x0")
    # turns this into a list of 1 byte chunks
    txn_data = pad_list(list(bytes.fromhex(normalize_hex_str(
        txn_data_no_segwit_hex))), 31*MAX_ENCODED_CHUNKS, 0)

    order_nonce_encoded = split_hex_into_31_byte_chunks(
        normalize_hex_str(order_nonce_hex))

    prover_toml_string = "\n".join(
        [
            f"txn_data_encoded={json.dumps(txn_data_encoded)}",
            f"lp_reservation_data_encoded={json.dumps(padded_lp_reservation_data_encoded)}",
            f"order_nonce_encoded={json.dumps(order_nonce_encoded)}",
            f"expected_payout={expected_payout}",
            f"lp_count={lp_count}",
            f"txn_data={json.dumps(txn_data)}",
        ]
    )

    print("Creating witness...")
    await create_witness(prover_toml_string, compilation_build_folder)


def hash_pairs(hex_str1, hex_str2):
    """Hash two hex strings together using double SHA-256 and return the hex result."""
    # Convert hex strings to binary, in little-endian format
    bin1 = bytes.fromhex(hex_str1)[::-1]
    bin2 = bytes.fromhex(hex_str2)[::-1]
    
    # Combine the binary data
    combined = bin1 + bin2
    
    # Double SHA-256 hashing
    hash_once = hashlib.sha256(combined).digest()
    hash_twice = hashlib.sha256(hash_once).digest()
    
    # Return the result as a hex string, in little-endian format
    return hash_twice[::-1].hex()

def generate_merkle_proof(txn_hashes: list, target_hash: str):
    """Generate a Merkle proof for the target hash."""
    proof = []
    target_index = txn_hashes.index(target_hash)
    depth = 0
    while len(txn_hashes) > 1:
        new_level = []
        if len(txn_hashes) % 2 == 1:
            txn_hashes.append(txn_hashes[-1])
        for i in range(0, len(txn_hashes), 2):
            left, right = txn_hashes[i], txn_hashes[i+1]
            if i <= target_index < i+2:
                if target_index == i:
                    proof.append((right, 'right'))
                else:
                    proof.append((left, 'left'))
                target_hash = hash_pairs(left, right)
            new_level.append(hash_pairs(left, right))
        txn_hashes = new_level
        target_index //= 2
        depth += 1
    
    return proof

def create_merkle_proof_toml(proof: list):
    toml_str = ""
    for i, (hash, direction) in enumerate(proof):
        flag = "true" if direction == 'right' else "false"
        toml_str += (f"[[proposed_merkle_proof]] # {i+1}\nhash = {list(bytes.fromhex(normalize_hex_str(hash)))}\ndirection = {flag}\n\n")
    
    # Determine how many padding entries are needed
    num_padding_entries = 20 - len(proof)
    
    # Padding with 0 u8 32-byte arrays if needed
    for j in range(num_padding_entries):
        toml_str += (f"[[proposed_merkle_proof]] # {len(proof) + j + 1}\nhash = {list(bytes.fromhex('00'*32))}\ndirection = false\n\n")

    return toml_str




"""
fn main(
    // Transaction Hash Verification
    txn_hash_encoded: pub [Field; 2],
    intermediate_hash_encoded_and_txn_data: [Field; constants::MAX_ENCODED_CHUNKS + 2],
    // Transaction Inclusion Verification
    proposed_merkle_root_encoded: [Field; 2],
    proposed_merkle_proof: [sha256_merkle::MerkleProofStep; 20],
    // Payment Verification + Lp Hash Verification
    lp_reservation_hash_encoded: pub [Field; 2],
    order_nonce_encoded: pub [Field; 2],
    expected_payout: pub u64,
    lp_count: pub u64,
    lp_reservation_data_flat_encoded: [Field; constants::MAX_LIQUIDITY_PROVIDERS*4],
    // Block Verification
    confirmation_block_hash_encoded: pub [Field; 2],
    proposed_block_hash_encoded: pub [Field; 2],
    safe_block_hash_encoded: pub [Field; 2],
    retarget_block_hash_encoded: pub [Field; 2],
    safe_block_height: pub u64,
    block_height_delta: pub u64,
    // Proof Data
    lp_hash_verification_key: [Field; 114],
    lp_hash_proof: [Field; 93],
    txn_hash_verification_key: [Field; 114],
    txn_hash_proof: [Field; 93],
    txn_hash_vk_hash_index: u64,
    payment_verification_key: [Field; 114],
    payment_proof: [Field; 93],
    block_verification_key: [Field; 114],
    block_proof: [Field; 93]
) {
"""

async def create_giga_circuit_prover_toml(
    txn_hash_hex: str,
    intermediate_hash_hex: str,
    txn_data_no_segwit_hex: str,
    proposed_merkle_root_hex: str,
    proposed_merkle_proof: list,
    lp_reservation_hash_hex: str,
    order_nonce_hex: str,
    expected_payout: int,
    lp_reservation_data: list[LiquidityProvider],
    confirmation_block_hash_hex: str,
    proposed_block_hash_hex: str,
    safe_block_hash_hex: str,
    retarget_block_hash_hex: str,
    safe_block_height: int,
    block_height_delta: int,
    lp_hash_verification_key: list[str],
    lp_hash_proof: list[str],
    txn_hash_verification_key: list[str],
    txn_hash_proof: list[str],
    txn_hash_vk_hash_index: int,
    payment_verification_key: list[str],
    payment_proof: list[str],
    block_verification_key: list[str],
    block_proof: list[str],
    compilation_build_folder: str
):
    txn_hash_encoded = split_hex_into_31_byte_chunks(normalize_hex_str(txn_hash_hex))
    intermediate_hash_encoded_and_txn_data = split_hex_into_31_byte_chunks(normalize_hex_str(intermediate_hash_hex)) + pad_list(split_hex_into_31_byte_chunks(normalize_hex_str(txn_data_no_segwit_hex)), MAX_ENCODED_CHUNKS, "0x0")
    proposed_merkle_root_encoded = split_hex_into_31_byte_chunks(normalize_hex_str(proposed_merkle_root_hex))

    lp_reservation_hash_encoded = split_hex_into_31_byte_chunks(normalize_hex_str(lp_reservation_hash_hex))
    order_nonce_encoded = split_hex_into_31_byte_chunks(normalize_hex_str(order_nonce_hex))
    lp_reservation_data_flat_encoded = sum(pad_list(
        list(map(lambda lp: split_hex_into_31_byte_chunks(
            eth_abi_encode(
                ["uint192", "uint64", "bytes32"],
                [lp.amount, lp.btc_exchange_rate, bytes.fromhex(
                    normalize_hex_str(lp.locking_script_hex))]
            ).hex()
        ), lp_reservation_data)),
        MAX_LIQUIDITY_PROVIDERS,
        ["0x0"] * 4
    ), [])
    prover_toml_string = "\n".join(
        [
            f"txn_hash_encoded={json.dumps(txn_hash_encoded)}",
            "",
            f"intermediate_hash_encoded_and_txn_data={json.dumps(intermediate_hash_encoded_and_txn_data)}",
            "",
            f"proposed_merkle_root_encoded={json.dumps(proposed_merkle_root_encoded)}",
            "",
            f"lp_reservation_hash_encoded={json.dumps(lp_reservation_hash_encoded)}",
            "",
            f"order_nonce_encoded={json.dumps(order_nonce_encoded)}",
            "",
            f"expected_payout={expected_payout}",
            "",
            f"lp_count={len(lp_reservation_data)}",
            "",
            f"lp_reservation_data_flat_encoded={json.dumps(lp_reservation_data_flat_encoded)}",
            "",
            f"confirmation_block_hash_encoded={json.dumps(split_hex_into_31_byte_chunks(normalize_hex_str(confirmation_block_hash_hex)))}",
            "",
            f"proposed_block_hash_encoded={json.dumps(split_hex_into_31_byte_chunks(normalize_hex_str(proposed_block_hash_hex)))}",
            "",
            f"safe_block_hash_encoded={json.dumps(split_hex_into_31_byte_chunks(normalize_hex_str(safe_block_hash_hex)))}",
            "",
            f"retarget_block_hash_encoded={json.dumps(split_hex_into_31_byte_chunks(normalize_hex_str(retarget_block_hash_hex)))}",
            "",
            f"safe_block_height={safe_block_height}",
            "",
            f"block_height_delta={block_height_delta}",
            "",
            f"lp_hash_verification_key={json.dumps(lp_hash_verification_key)}",
            "",
            f"lp_hash_proof={json.dumps(lp_hash_proof)}",
            "",
            f"txn_hash_verification_key={json.dumps(txn_hash_verification_key)}",
            "",
            f"txn_hash_proof={json.dumps(txn_hash_proof)}",
            "",
            f"txn_hash_vk_hash_index={txn_hash_vk_hash_index}",
            "",
            f"payment_verification_key={json.dumps(payment_verification_key)}",
            "",
            f"payment_proof={json.dumps(payment_proof)}",
            "",
            f"block_verification_key={json.dumps(block_verification_key)}",
            "",
            f"block_proof={json.dumps(block_proof)}",
            "",
            "",
            create_merkle_proof_toml(proposed_merkle_proof),
            "",
        ]
    )

    print("Creating witness...")
    await create_witness(prover_toml_string, compilation_build_folder)


async def load_recursive_sha_circuit(circuit_path: str):
# Load the circuit file
    async with aiofiles.open(circuit_path, "r") as file:
        return {
            "src/main.nr": await file.read()
        }


async def initialize_recursive_sha_build_folder(bytelen: int, circuit_path: str):
    NAME = "dynamic_sha_lib"
    sha_circuit_fs = await load_recursive_sha_circuit(circuit_path)
    lines = sha_circuit_fs['src/main.nr'].split("\n")
    for i, line in enumerate(lines):
        if "[REPLACE]" in line:
            # global BYTELEN: u32 = 7000; // [REPLACE]
            lines[i] = f"global BYTELEN: u32 = {bytelen};"
            break
    subcircuit_source = "\n".join(lines)
    return await initialize_noir_project_folder({
        'src/main.nr': subcircuit_source,
    }, NAME)


async def create_recursive_sha_witness(normalized_hex_str: str, max_chunks: int, compilation_dir: str):
    data_hash = hashlib.sha256(bytes.fromhex(normalized_hex_str)).hexdigest()
    encoded_data = pad_list(
        split_hex_into_31_byte_chunks(normalized_hex_str), max_chunks, "0x00"
    )
    expected_hash_encoded = split_hex_into_31_byte_chunks(data_hash)

    output = f"encoded_data={json.dumps(encoded_data)}\nexpected_hash_encoded={json.dumps(expected_hash_encoded)}"
    await create_witness(output, compilation_dir)


async def extract_cached_recursive_sha_vkey_data(
    bytelen: int, chunk_file: str
) -> tuple[str, list[str], str]:
    async with aiofiles.open(chunk_file, "r") as file:
        blob = json.loads(await file.read())[str(bytelen)]
        return (blob["vk_as_fields"][0], blob["vk_as_fields"][1:], blob["vk_bytes"])


def validate_bytelen(bytelen: int, max_bytes: int):
    if bytelen > MAX_ENCODED_CHUNKS*31 or bytelen < 1:
        raise Exception("Invalid bytelength")


def get_chunk_file_name(chunk_id: int):
    return f"vk_chunk_{chunk_id:04d}.json"



async def build_recursive_sha256_proof_and_input(
    data_hex_str: str,
    circuit_path: str = "circuits/recursive_sha/src/main.nr",
    chunk_folder: str = "generated_sha_circuits/",
    max_bytes: int = 7000,
    max_chunks: int = 226
) -> RecursiveSha256ProofArtifact:
    data = normalize_hex_str(data_hex_str)

    bytelen = len(data) // 2

    validate_bytelen(bytelen, max_bytes)

    vkey_hash, vkey_as_fields, vk_hexstr_bytes = await extract_cached_recursive_sha_vkey_data(
        bytelen,
        os.path.join(
            chunk_folder, get_chunk_file_name(math.floor((bytelen - 1) / 1000))
        ),
    )
    build_folder = await initialize_recursive_sha_build_folder(bytelen, circuit_path)

    vk_file = "public_input_proxy_vk"
    async with aiofiles.open(os.path.join(build_folder.name, vk_file), "wb+") as f:
        await f.write(bytes.fromhex(vk_hexstr_bytes))

    print("Compiling recursive sha256 circuit...")
    await compile_project(build_folder.name)
    print("Creating witness...")
    await create_recursive_sha_witness(data, max_chunks, build_folder.name)
    public_inputs_as_fields, proof_as_fields = await create_proof(
        vk_file,
        int.from_bytes(bytes.fromhex(
            normalize_hex_str(vkey_as_fields[4])), "big"),
        build_folder.name,
        BB,
    )
    print("SHA256 proof created!")
    build_folder.cleanup()
    return RecursiveSha256ProofArtifact(
        verification_key=vkey_as_fields,
        proof=proof_as_fields,
        public_inputs=public_inputs_as_fields,
        key_hash_index=bytelen - 1,
        key_hash=vkey_hash,
    )


async def build_recursive_lp_hash_proof_and_input(
    lps: list[LiquidityProvider],
    circuit_path: str = "circuits/lp_hash_verification",
    verify: bool = False
):
    print("Compiling lp hash verification circuit...")
    await compile_project(circuit_path)
    # [1] create prover toml and witness
    print("Creating prover toml and witness...")
    await create_lp_hash_verification_prover_toml(
        lp_reservation_data=lps,
        compilation_build_folder=circuit_path
    )
    # [3] build verification key, create proof, and verify proof
    vk_file = "./target/vk"
    print("Building verification key...")
    await build_raw_verification_key(vk_file, circuit_path, BB)
    print("Creating proof...")
    public_inputs_as_fields, proof_as_fields = await create_proof(
        pub_inputs=703,
        vk_path=vk_file,
        compilation_dir=circuit_path,
        bb_binary=BB
    )
    encoded_vkey = await extract_vk_as_fields(vk_file, circuit_path, BB)
    vkey_hash, vkey_as_fields = encoded_vkey[0], encoded_vkey[1:]
    if verify:
        print("Verifying proof...")
        await verify_proof(vk_path=vk_file, compilation_dir=circuit_path, bb_binary=BB)

    print("LP Hash proof gen successful!")

    return RecursiveProofArtifact(
        verification_key=vkey_as_fields,
        proof=proof_as_fields,
        public_inputs=public_inputs_as_fields,
        key_hash=vkey_hash
    )


async def build_recursive_payment_proof_and_input(
    lps: list[LiquidityProvider],
    txn_data_no_segwit_hex: str,
    order_nonce_hex: str,
    expected_payout: int,
    circuit_path: str = "circuits/payment_verification",
    verify: bool = False
    ):
    print("Compiling payment verification circuit...")
    await compile_project(circuit_path)
    # [1] create prover toml and witnesses
    print("Creating prover toml and witness...")
    await create_payment_verification_prover_toml(
        txn_data_no_segwit_hex=txn_data_no_segwit_hex,
        lp_reservation_data=lps,
        lp_count=len(lps),
        order_nonce_hex=order_nonce_hex,
        expected_payout=expected_payout,
        compilation_build_folder=circuit_path
    )
    # [3] build verification key, create proof, and verify Proof
    vk = "./target/vk"
    print("Building verification key...")
    await build_raw_verification_key(vk, circuit_path, BB)
    print("Creating proof...")
    public_inputs_as_fields, proof_as_fields = await create_proof(pub_inputs=930, vk_path=vk, compilation_dir=circuit_path, bb_binary=BB)
    if verify:
        print("Verifying proof...")
        await verify_proof(vk_path=vk, compilation_dir=circuit_path, bb_binary=BB)
    print("Payment verification proof gen successful!")
    encoded_vkey = await extract_vk_as_fields(vk, circuit_path, BB)
    vkey_hash, vkey_as_fields = encoded_vkey[0], encoded_vkey[1:]
    return RecursiveProofArtifact(
        verification_key=vkey_as_fields,
        proof=proof_as_fields,
        public_inputs=public_inputs_as_fields,
        key_hash=vkey_hash
    )




async def create_block_pair_verification_prover_toml(
    block_1: Block,
    block_2: Block,
    last_retarget_block: Block,
    next_retarget_block: Union[Block, None] = None,
    next_retarget_verification_key: Union[list[str], None] = None,
    next_retarget_proof: Union[list[str], None] = None,
    compilation_build_folder: str = "circuits/block_verification/pair_block_verification"
    ):
    print("Generating prover toml...")
    if next_retarget_block is None:
        next_retarget_block = NULL_BLOCK
        next_retarget_hash = "0x" + ("00" * 32)
    else:
        next_retarget_hash = compute_block_hash(next_retarget_block)
    if next_retarget_verification_key is None:
        next_retarget_verification_key = ["0x0"] * 114
    if next_retarget_proof is None:
        next_retarget_proof = ["0x0"] * 93

    prover_toml_string = "\n".join(
        [
            "block_hash_1=" + json.dumps(hex_string_to_byte_array(compute_block_hash(block_1))),
            "block_hash_2=" + json.dumps(hex_string_to_byte_array(compute_block_hash(block_2))),
            "last_retarget_block_hash=" + json.dumps(hex_string_to_byte_array(compute_block_hash(last_retarget_block))),
            "block_height_1=" + str(block_1.height),
            "block_height_2=" + str(block_2.height),
            "last_retarget_block_height=" + str(last_retarget_block.height),
            "is_buffer=" + str(block_1.height == block_2.height).lower(),
            "",
            "next_retarget_hash=" + json.dumps(hex_string_to_byte_array(next_retarget_hash)),
            "",
            "next_retarget_verification_key=" + json.dumps(next_retarget_verification_key),
            "",
            "next_retarget_proof=" + json.dumps(next_retarget_proof),
            "",
            "[block_header_1]",
            *await block_toml_encoder(block_1),
            "",
            "[block_header_2]",
            *await block_toml_encoder(block_2),
            "",
            "[last_retarget_block]",
            *await block_toml_encoder(last_retarget_block),
            "",
            "[next_retarget_header]",
            *await block_toml_encoder(next_retarget_block),
            "",
        ]
    )

    print("Creating witness...")
    await create_witness(prover_toml_string, compilation_build_folder)


# this requires the tree circuits to be precompiled
async def build_block_tree_circuit_prover_toml(
    first_block_hash_hex: str,
    last_block_hash_hex: str,
    first_block_height: int,
    last_block_height: int,
    first_is_buffer: bool,
    last_is_buffer: bool,
    last_retarget_block_hash_hex: str,
    last_retarget_block_height: int,
    link_block_hash_hex: str,
    first_pair_verification_key: list[str],
    first_pair_proof: list[str],
    last_pair_verification_key: list[str],
    last_pair_proof: list[str],
    circuit_path: str = "circuits/block_verification/base_block_tree",
    safe_concurrent: bool = False
    ):

    original_circuit_path = circuit_path
    if safe_concurrent:
        temp_dir = tempfile.TemporaryDirectory()
        # Copy the entire 'circuits' directory
        shutil.copytree("circuits", os.path.join(temp_dir.name, "circuits"))
        # Update circuit_path to point to the correct location in the temp directory
        circuit_path = os.path.join(temp_dir.name, original_circuit_path)
        # Ensure the directory exists
        os.makedirs(circuit_path, exist_ok=True) 

    print("Generating prover toml...")
    prover_toml_string = "\n".join(
        [
            "first_block_hash=" + json.dumps(hex_string_to_byte_array(first_block_hash_hex)),
            "last_block_hash=" + json.dumps(hex_string_to_byte_array(last_block_hash_hex)),
            "first_block_height=" + str(first_block_height),
            "last_block_height=" + str(last_block_height),
            "first_is_buffer=" + str(first_is_buffer).lower(),
            "last_is_buffer=" + str(last_is_buffer).lower(),
            "last_retarget_block_hash=" + json.dumps(hex_string_to_byte_array(last_retarget_block_hash_hex)),
            "last_retarget_block_height=" + str(last_retarget_block_height),
            "link_block_hash=" + json.dumps(hex_string_to_byte_array(link_block_hash_hex)),
            "",
            "first_pair_verification_key=" + json.dumps(first_pair_verification_key),
            "",
            "first_pair_proof=" + json.dumps(first_pair_proof),
            "",
            "last_pair_verification_key=" + json.dumps(last_pair_verification_key),
            "",
            "last_pair_proof=" + json.dumps(last_pair_proof),
            "",
        ]
    )

    print("Creating witness...")
    witness_bytes = await create_witness(prover_toml_string, circuit_path)
    return witness_bytes

@cache
async def get_base_tree_circuit_verification_hash(compilation_dir: str):
    vk = tempfile.NamedTemporaryFile()
    await compile_project(compilation_dir)
    await build_raw_verification_key(vk.name, compilation_dir, BB)
    vk_data = await extract_vk_as_fields(vk.name, compilation_dir, BB)
    return vk_data[0]

async def build_block_tree_base_proof_and_input(
    first_block: Block,
    last_block: Block,
    last_retarget_block: Block,
    link_block: Block,
    first_pair_proof: SubBlockArtifact,
    last_pair_proof: SubBlockArtifact,
    circuit_path: str = "circuits/block_verification/base_block_tree",
    ):

    temp_dir = tempfile.TemporaryDirectory()
    shutil.copytree("circuits", os.path.join(temp_dir.name, "circuits"))
    circuit_path = os.path.join(temp_dir.name, circuit_path)
    os.makedirs(circuit_path, exist_ok=True)
    
    await build_block_tree_circuit_prover_toml(
        first_block_hash_hex=compute_block_hash(first_block),
        last_block_hash_hex=compute_block_hash(last_block),
        first_block_height=first_block.height,
        last_block_height=last_block.height,
        first_is_buffer=first_block.height == link_block.height,
        last_is_buffer=link_block.height == last_block.height,
        last_retarget_block_hash_hex=compute_block_hash(last_retarget_block),
        last_retarget_block_height=last_retarget_block.height,
        link_block_hash_hex=compute_block_hash(link_block),
        first_pair_verification_key=first_pair_proof.proof_artifact.verification_key,
        first_pair_proof=first_pair_proof.proof_artifact.proof,
        last_pair_verification_key=last_pair_proof.proof_artifact.verification_key,
        last_pair_proof=last_pair_proof.proof_artifact.proof,
        circuit_path=circuit_path
    )
    await compile_project(circuit_path)
    # [3] build verification key, create proof, and verify Proof
    vk = "./target/vk"
    print("Building verification key...")
    await build_raw_verification_key(vk, circuit_path, BB)
    print("Creating proof...")
    public_inputs_as_fields, proof_as_fields = await create_proof(pub_inputs=101, vk_path=vk, compilation_dir=circuit_path, bb_binary=BB)
    print("Block tree base proof gen successful!")
    encoded_vkey = await extract_vk_as_fields(vk, circuit_path, BB)
    vkey_hash, vkey_as_fields = encoded_vkey[0], encoded_vkey[1:]
    return SubBlockArtifact(
        proof_artifact=RecursiveProofArtifact(
            verification_key=vkey_as_fields,
            proof=proof_as_fields,
            public_inputs=public_inputs_as_fields,
            key_hash=vkey_hash
        ),
        first_block=first_block,
        last_block=last_block
    )


async def height_to_recursive_tree_vk_hash(height: int, circuit_path: str = "circuits/block_verification/base_block_tree"):
    base_tree_vk_hash = await get_base_tree_circuit_verification_hash(circuit_path)
    if height == 2:
        return base_tree_vk_hash
    async with aiofiles.open(f"generated_block_tree_circuits/block_tree_height_{height}.json") as f:
        tree_data = json.loads(await f.read())
        return tree_data["vk_hash"]

# 478265
async def build_block_tree_proof_and_input(
    first_block: Block,
    last_block: Block,
    last_retarget_block: Block,
    link_block: Block,
    first_pair_proof: RecursiveProofArtifact,
    last_pair_proof: RecursiveProofArtifact,
    height: int,
    circuit_path: str = "circuits/block_verification/base_block_tree",
):
    # height 0 is technically pair circuits
    # height 1 is custom to handle pair circuits directly
    if height == 1:
        return await build_block_tree_base_proof_and_input(
            first_block=first_block,
            last_block=last_block,
            last_retarget_block=last_retarget_block,
            link_block=link_block,
            first_pair_proof=first_pair_proof,
            last_pair_proof=last_pair_proof,
            circuit_path=circuit_path
        )

    temp_dir = tempfile.TemporaryDirectory()
    shutil.copytree("circuits", os.path.join(temp_dir.name, "circuits"))
    circuit_path = os.path.join(temp_dir.name, circuit_path)
    os.makedirs(circuit_path, exist_ok=True)
    child_vk_hash = await height_to_recursive_tree_vk_hash(height, circuit_path)

    async with aiofiles.open("circuits/block_verification/recursive_block_tree/src/main.nr") as f:
        circuit_str = await f.read()
    lines = circuit_str.split("\n")
    for j, line in enumerate(lines):
        if "[REPLACE]" in line:
            lines[j] = f"global BLOCK_TREE_CIRCUIT_KEY_HASH: Field = {child_vk_hash};"
            break
    subcircuit_source = "\n".join(lines)
    async with aiofiles.open(os.path.join(circuit_path, "src/main.nr"), "w") as f:
        await f.write(subcircuit_source)

    print("Compiling block tree verification circuit...")
    await compile_project(circuit_path)
    
    await build_block_tree_circuit_prover_toml(
        first_block_hash_hex=compute_block_hash(first_block),
        last_block_hash_hex=compute_block_hash(last_block),
        first_block_height=first_block.height,
        last_block_height=last_block.height,
        first_is_buffer=first_block.height == last_block.height,
        last_is_buffer=last_block.height == last_block.height,
        last_retarget_block_hash_hex=compute_block_hash(last_retarget_block),
        last_retarget_block_height=last_retarget_block.height,
        link_block_hash_hex=compute_block_hash(link_block),
        first_pair_verification_key=first_pair_proof.verification_key,
        first_pair_proof=first_pair_proof.proof,
        last_pair_verification_key=last_pair_proof.verification_key,
        last_pair_proof=last_pair_proof.proof,
        circuit_path=circuit_path
    )
    # [3] build verification key, create proof, and verify Proof
    vk = "./target/vk"
    print("Building verification key...")
    await build_raw_verification_key(vk, circuit_path, BB)
    print("Creating proof...")
    public_inputs_as_fields, proof_as_fields = await create_proof(pub_inputs=101, vk_path=vk, compilation_dir=circuit_path, bb_binary=BB)
    print("Block tree proof gen successful!")
    encoded_vkey = await extract_vk_as_fields(vk, circuit_path, BB)
    vkey_hash, vkey_as_fields = encoded_vkey[0], encoded_vkey[1:]
    return SubBlockArtifact(
        proof_artifact=RecursiveProofArtifact(
            verification_key=vkey_as_fields,
            proof=proof_as_fields,
            public_inputs=public_inputs_as_fields,
            key_hash=vkey_hash
        ),
        first_block=first_block,
        last_block=last_block
    )


async def build_block_pair_proof_input(
    block_1: Block,
    block_2: Block,
    last_retarget_block: Block,
    next_retarget_block: Union[Block, None] = None,
    next_retarget_verification_key: Union[list[str], None] = None,
    next_retarget_proof: Union[list[str], None] = None,
    circuit_path: str = "circuits/block_verification/pair_block_verification",
    safe_concurrent: bool = False
):
    original_circuit_path = circuit_path
    if safe_concurrent:
        temp_dir = tempfile.TemporaryDirectory()
        # Copy the entire 'circuits' directory
        shutil.copytree("circuits", os.path.join(temp_dir.name, "circuits"))
        # Update circuit_path to point to the correct location in the temp directory
        circuit_path = os.path.join(temp_dir.name, original_circuit_path)
        # Ensure the directory exists
        os.makedirs(circuit_path, exist_ok=True) 

    print("Compiling block pair verification circuit...")
    await compile_project(circuit_path)
    
    print("Creating prover toml and witness...")
    await create_block_pair_verification_prover_toml(
        block_1=block_1,
        block_2=block_2,
        last_retarget_block=last_retarget_block,
        next_retarget_block=next_retarget_block,
        next_retarget_verification_key=next_retarget_verification_key,
        next_retarget_proof=next_retarget_proof,
        compilation_build_folder=circuit_path
    )
    
    vk = "./target/vk"
    print("Building verification key...")
    await build_raw_verification_key(vk, circuit_path, BB)
    
    print("Creating proof...")
    public_inputs_as_fields, proof_as_fields = await create_proof(pub_inputs=100, vk_path=vk, compilation_dir=circuit_path, bb_binary=BB)
    
    print("Block pair proof gen successful!")
    encoded_vkey = await extract_vk_as_fields(vk, circuit_path, BB)
    vkey_hash, vkey_as_fields = encoded_vkey[0], encoded_vkey[1:]
    

    return SubBlockArtifact(
        proof_artifact=RecursiveProofArtifact(
            verification_key=vkey_as_fields,
            proof=proof_as_fields,
            public_inputs=public_inputs_as_fields,
            key_hash=vkey_hash
        ),
        first_block=block_1,
        last_block=block_2
    )


async def proof_gen_semaphore_wrapper(semaphore: asyncio.BoundedSemaphore, coro: Coroutine[Any, Any, T], id: Any) -> T:
    async with semaphore:
        print(f"Generating proof {id}")
        return await coro


# entry point for building block proofs
async def build_block_proof_and_input(
    blocks: list[Block],
    last_retarget_block: Block,
    max_concurrent_proof_gen: int = 10
    ):
    for block in blocks:
        assert block.height - (block.height % 2016) == last_retarget_block.height

    r = math.ceil(math.log2(len(blocks)))
    print("Max Tree Height: ", r) 

    proof_coros = []
    block_pairs = []
    bounded_semaphore = asyncio.BoundedSemaphore(max_concurrent_proof_gen)
    
    print("Generating pair proofs...")
    for i, _ in enumerate(blocks):
        if i == len(blocks)-1:
            break
        coro = build_block_pair_proof_input(
            blocks[i],
            blocks[i+1],
            last_retarget_block,
            safe_concurrent=True
        )
        block_pairs.append((blocks[i], blocks[i+1]))
        proof_coros.append(proof_gen_semaphore_wrapper(bounded_semaphore, coro, f"{i+1}/{len(blocks)-1}"))
    
    pair_proofs = await asyncio.gather(*proof_coros)

    # if there is only one pair, return the proof and block pair b/c no need to roll anything up
    if len(pair_proofs) == 1:
        return BlockTreeArtifact(
            height=0,
            proof_artifact=pair_proofs[0].proof_artifact,
            first_block=pair_proofs[0].first_block,
            last_block=pair_proofs[0].last_block
        )
    if len(pair_proofs) == 2: # no buffer blocks needed
        buffered_pair_proofs = pair_proofs
        buffered_block_pairs = block_pairs
    else:
        # now we need to generate the buffer block needed to get from n to 2^r
        # easiest to just take the block right before the last block, so the last block can be the last pair 
        # effectively making buffer blocks purely internal
        buffer_proof = await build_block_pair_proof_input(
            blocks[-2],
            blocks[-2],
            last_retarget_block,
            safe_concurrent=True
        )
        buffer_count = 2**r - len(pair_proofs)
        buffered_pair_proofs = pair_proofs[:-1] + [buffer_proof]*buffer_count + [pair_proofs[-1]]
        buffered_block_pairs = block_pairs[:-1] + [(blocks[-2], blocks[-2])]*buffer_count + [block_pairs[-1]]

    print("PAIR CIRCUITS GENERATED")

    # first do the base tree proofs
    base_tree_proofs = []
    base_tree_pairs = []
    for i in range(0, len(buffered_pair_proofs), 2):
        coro = build_block_tree_base_proof_and_input(
            first_block=buffered_block_pairs[i][0],
            last_block=buffered_block_pairs[i+1][1],
            last_retarget_block=last_retarget_block,
            link_block=buffered_block_pairs[i+1][0],
            first_pair_proof=buffered_pair_proofs[i],
            last_pair_proof=buffered_pair_proofs[i+1],
            circuit_path="circuits/block_verification/base_block_tree"
        )
        base_tree_pairs.append((buffered_block_pairs[i][0], buffered_block_pairs[i+1][1]))
        base_tree_proofs.append(proof_gen_semaphore_wrapper(bounded_semaphore, coro, f"{i+1}/{len(buffered_pair_proofs)//2}"))

    base_tree_proof_artifacts = await asyncio.gather(*base_tree_proofs)
    if len(base_tree_proof_artifacts) == 1:
        return BlockTreeArtifact(
            height=1,
            proof_artifact=base_tree_proof_artifacts[0].proof_artifact,
            first_block=base_tree_proof_artifacts[0].first_block,
            last_block=base_tree_proof_artifacts[0].last_block
        )
    print("NEED TO GENERATE TREE CIRCUITS")

    """

    # Now rollup the pairs into a tree
    current_pairs = buffered_pair_proofs
    current_block_pairs = buffered_block_pairs
    for i in range(r):
        print(f"Generating tree level {i+1}...")
        next_pairs = []

        current_pairs = await asyncio.gather(*next_pairs)
        current_block_pairs = [(current_block_pairs[j][0], current_block_pairs[j+1][1]) for j in range(0, len(current_block_pairs), 2)]

    print("Final Proof and Block pairs")
    print(current_pairs)
    print(current_block_pairs)
    """



async def build_giga_circuit_proof_and_input(
    txn_data_no_segwit_hex: str,
    lp_reservations: list[LiquidityProvider],
    proposed_block_header: Block,
    safe_block_header: Block,
    retarget_block_header: Block,
    inner_block_headers: list[Block],
    confirmation_block_headers: list[Block],
    order_nonce_hex: str,
    expected_payout: int,
    safe_block_height: int,
    block_height_delta: int,
    circuit_path: str = "circuits/giga"
    ):
    # [1] compile giga 
    
    # [2] build recursive proofs and inputs 
    sha_recursive_artifact = await build_recursive_sha256_proof_and_input(
        data_hex_str=txn_data_no_segwit_hex
    )
    print()
    lp_hash_recursive_artifact = await build_recursive_lp_hash_proof_and_input(
        lps=lp_reservations
    )
    print()
    print()
    payment_recursive_artifact = await build_recursive_payment_proof_and_input(
        lps=lp_reservations,
        txn_data_no_segwit_hex=txn_data_no_segwit_hex,
        order_nonce_hex=order_nonce_hex,
        expected_payout=expected_payout
    )
    print()


    # [3] create prover toml and witnesses
    intermediate_hash_hex = hashlib.sha256(bytes.fromhex(normalize_hex_str(txn_data_no_segwit_hex))).hexdigest()
    txn_hash_hex = hashlib.sha256(bytes.fromhex(intermediate_hash_hex)).hexdigest()

    print("Generating merkle proof...")
    merkle_proof = generate_merkle_proof(
        txn_hashes=list(map(lambda hash: normalize_hex_str(hash), proposed_block_header.txns)),
        target_hash=bytes.fromhex(normalize_hex_str(txn_hash_hex))[::-1].hex()
    )
    print()

    confirmation_block_hash_hex = compute_block_hash(confirmation_block_headers[-1])
    proposed_block_hash_hex = compute_block_hash(proposed_block_header)
    safe_block_hash_hex = compute_block_hash(safe_block_header)
    retarget_block_hash_hex = compute_block_hash(retarget_block_header)
    

    print("Compiling giga circuit...")
    await compile_project(circuit_path)
    print("Creating prover toml and witness...")
    await create_giga_circuit_prover_toml(
        txn_hash_hex=txn_hash_hex,
        intermediate_hash_hex=intermediate_hash_hex,
        txn_data_no_segwit_hex=txn_data_no_segwit_hex,
        proposed_merkle_root_hex=proposed_block_header.merkle_root,
        proposed_merkle_proof=merkle_proof,
        lp_reservation_hash_hex=lp_hash_recursive_artifact.key_hash,
        order_nonce_hex=order_nonce_hex,
        expected_payout=expected_payout,
        lp_reservation_data=lp_reservations,
        confirmation_block_hash_hex=confirmation_block_hash_hex,
        proposed_block_hash_hex=proposed_block_hash_hex,
        safe_block_hash_hex=safe_block_hash_hex,
        retarget_block_hash_hex=retarget_block_hash_hex,
        safe_block_height=safe_block_height,
        block_height_delta=block_height_delta,
        lp_hash_verification_key=lp_hash_recursive_artifact.verification_key,
        lp_hash_proof=lp_hash_recursive_artifact.proof,
        txn_hash_verification_key=sha_recursive_artifact.verification_key,
        txn_hash_proof=sha_recursive_artifact.proof,
        txn_hash_vk_hash_index=sha_recursive_artifact.key_hash_index,
        payment_verification_key=payment_recursive_artifact.verification_key,
        payment_proof=payment_recursive_artifact.proof,
        block_verification_key=block_recursive_artifact.verification_key,
        block_proof=block_recursive_artifact.proof,
        compilation_build_folder=circuit_path
    )

    print("Creating proof...")
    proof_hex = normalize_hex_str(await create_solidity_proof(project_name="giga", compilation_dir=circuit_path))
    print("Giga circuit proof gen successful!")
    return SolidityProofArtifact(
        proof=proof_hex
    )


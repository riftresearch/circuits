mod errors;
use bitcoin::hashes::Hash;
use errors::{BitcoinRpcError};
use std::fmt::Write;
use bitcoin::hashes::hex::FromHex;


use rift_lib::sha256_merkle::{MerkleProofStep, hash_pairs};
use rift_lib::bitcoin::Block as RiftOptimizedBlock;

pub fn load_hex_bytes(file: &str) -> Vec<u8> {
    let hex_string = std::fs::read_to_string(file).expect("Failed to read file");
    Vec::<u8>::from_hex(&hex_string).expect("Failed to parse hex")
}

pub fn to_little_endian<const N: usize>(input: [u8; N]) -> [u8; N] {
    let mut output = [0; N];
    for (i, &byte) in input.iter().enumerate() {
        output[N - 1 - i] = byte;
    }
    output
}

pub fn to_hex_string(bytes: &[u8; 32]) -> String {
    let mut s = String::with_capacity(64);
    for &b in bytes {
        write!(&mut s, "{:02x}", b).unwrap();
    }
    s
}

pub fn get_retarget_height_from_block_height(block_height: u64) -> u64 {
    block_height - (block_height % 2016)
}


// Expects leaves to be in little-endian format (as shown on explorers)
pub fn generate_merkle_proof_and_root(leaves: Vec<[u8; 32]>, desired_leaf: [u8; 32]) -> (Vec<MerkleProofStep>, [u8; 32]) {
    let mut current_level = leaves;
    let mut proof: Vec<MerkleProofStep> = Vec::new();
    let mut desired_index = current_level.iter().position(|&leaf| leaf == desired_leaf)
        .expect("Desired leaf not found in the list of leaves");

    while current_level.len() > 1 {
        let mut next_level = Vec::new();
        let mut i = 0;

        while i < current_level.len() {
            let left = current_level[i];
            let right = if i + 1 < current_level.len() {
                current_level[i + 1]
            } else {
                left
            };

            let parent_hash = hash_pairs(left, right);
            next_level.push(parent_hash);

            if i == desired_index || i + 1 == desired_index {
                let proof_step = if i == desired_index {
                    MerkleProofStep {
                        hash: right,
                        direction: true,
                    }
                } else {
                    MerkleProofStep {
                        hash: left,
                        direction: false,
                    }
                };
                proof.push(proof_step);
                desired_index /= 2;
            }

            i += 2;
        }

        current_level = next_level;
    }

    let merkle_root = current_level[0];
    (proof, merkle_root)
}


pub fn to_rift_optimized_block(height: u64, block: &bitcoin::Block) -> RiftOptimizedBlock {
    RiftOptimizedBlock {
        height,
        version: block.header.version.to_consensus().to_le_bytes(),
        prev_blockhash: block.header.prev_blockhash.to_raw_hash().to_byte_array(),
        merkle_root: block.header.merkle_root.to_raw_hash().to_byte_array(),
        time: block.header.time.to_le_bytes(),
        bits: block.header.bits.to_consensus().to_le_bytes(),
        nonce: block.header.nonce.to_le_bytes(),
    }
}


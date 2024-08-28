#[cfg(test)]
mod tests {
    use bitcoin::consensus::encode::{deserialize, serialize};
    use bitcoin::hashes::Hash;
    use bitcoin::hex::{DisplayHex, FromHex};
    use bitcoin::Block;

    use rift_lib::sha256_merkle::{
        assert_merkle_proof_equality, MerkleProofStep
    };
    use utils::{to_little_endian, load_hex_bytes, generate_merkle_proof_and_root};

    #[test]
    fn test_real_merkle_root() {
        let block = deserialize::<Block>(&load_hex_bytes("data/block_858564.hex")).unwrap();
        let le_merkle_root = to_little_endian(*block.header.merkle_root.as_byte_array());
        let txn_index = 5;
        let txn = to_little_endian(
            *block.txdata[txn_index]
                .compute_txid()
                .as_raw_hash()
                .as_byte_array(),
        );

        let ( merkle_proof, calculated_merkle_root) = generate_merkle_proof_and_root(
            block
                .txdata
                .iter()
                .map(|tx| to_little_endian(*tx.compute_txid().as_raw_hash().as_byte_array()))
                .collect(),
            txn,
        );


        println!("Calculated Merkle Root: {:?}", calculated_merkle_root.as_hex());
        println!("Known Merkle Root:      {:?}", le_merkle_root.as_hex());

        assert_eq!(
            calculated_merkle_root,
            le_merkle_root,
            "Invalid merkle root"
        );

        assert_merkle_proof_equality(
            calculated_merkle_root,
            txn,
            merkle_proof.as_slice()
        );

    }

    // run the test but then try to validate the proof with a different tx
    // expect it to fail
    #[test]
    #[should_panic(expected = "Merkle proof verification failed")]
    fn test_real_merkle_root_invalid_verification_txn() {
        let block = deserialize::<Block>(&load_hex_bytes("data/block_858564.hex")).unwrap();
        let le_merkle_root = to_little_endian(*block.header.merkle_root.as_byte_array());
        let txn_index = 5;
        let txn = to_little_endian(
            *block.txdata[txn_index]
                .compute_txid()
                .as_raw_hash()
                .as_byte_array(),
        );
        let different_txn = to_little_endian(
            *block.txdata[txn_index + 1]
                .compute_txid()
                .as_raw_hash()
                .as_byte_array(),
        );

        let ( merkle_proof, calculated_merkle_root) = generate_merkle_proof_and_root(
            block
                .txdata
                .iter()
                .map(|tx| to_little_endian(*tx.compute_txid().as_raw_hash().as_byte_array()))
                .collect(),
            txn,
        );


        println!("Calculated Merkle Root: {:?}", calculated_merkle_root.as_hex());
        println!("Known Merkle Root:      {:?}", le_merkle_root.as_hex());

        assert_eq!(
            calculated_merkle_root,
            le_merkle_root,
            "Invalid merkle root"
        );

        assert_merkle_proof_equality(
            calculated_merkle_root,
            different_txn,
            merkle_proof.as_slice()
        );


    }
}

//! OpenAI Standard API text list prices, verified 2026-11-07.
//! https://developers.openai.com/api/docs/pricing
use serde::{Deserialize, Serialize};

pub(crate) const PRICES_AS_OF: &str = "2026-11-07";

#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Serialize)]
pub(crate) enum Model {
    Astra,
    Sol61,
    Terra61,
    Sol6,
    Luna,
}

impl Model {
    pub(crate) fn from_id(id: &str) -> Option<Self> {
        match id {
            "gpt-6-astra" => Some(Self::Astra),
            "gpt-6.1-sol" => Some(Self::Sol61),
            "gpt-6.1-terra" => Some(Self::Terra61),
            "gpt-6-sol" => Some(Self::Sol6),
            "gpt-6-luna" => Some(Self::Luna),
            _ => None,
        }
    }

    pub(crate) fn standard_usd(self, input: u64, read: u64, write: u64, output: u64) -> f64 {
        // Rates per million tokens. The local log does not distinguish write
        // lifetimes, so use the documented 30-minute API rate.
        let (input_rate, read_rate, write_rate, output_rate) = match self {
            Self::Astra => (10.0, 1.0, 12.5, 50.0),
            Self::Sol61 => (2.0, 0.10, 2.5, 10.0),
            Self::Terra61 => (1.0, 0.05, 1.25, 5.0),
            Self::Sol6 => (2.0, 0.20, 2.5, 10.0),
            Self::Luna => (0.10, 0.01, 0.125, 0.50),
        };
        let ordinary = input - read - write;
        let input_cost =
            ordinary as f64 * input_rate + read as f64 * read_rate + write as f64 * write_rate;
        let output_cost = output as f64 * output_rate;
        let cost = if input > 272_000 {
            2.0 * input_cost + 1.5 * output_cost
        } else {
            input_cost + output_cost
        };
        cost / 1_000_000.0
    }
}

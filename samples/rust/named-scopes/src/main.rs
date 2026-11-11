// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.

use clap::Parser;
use std::process::ExitCode;
use webiq_scopes::{
    Arguments, AuthSettings, Command, HttpTransport, Result, ScopesClient, execute, required,
    validate_command,
};

async fn run() -> Result<String> {
    let command = Arguments::parse().command;
    let definition = validate_command(&command)?;
    let getenv = |name: &str| std::env::var(name).ok();
    let account = if matches!(command, Command::Discover | Command::Search { .. }) {
        None
    } else {
        Some(required(&getenv, "WEBIQ_BILLING_ACCOUNT_ID")?)
    };
    let auth = AuthSettings::read(&command, &getenv)?
        .authenticate()
        .await?;
    let client = ScopesClient::new(HttpTransport::new()?, auth);
    execute(&client, &command, account.as_deref(), definition.as_ref()).await
}

#[tokio::main]
async fn main() -> ExitCode {
    match run().await {
        Ok(output) => {
            println!("{output}");
            ExitCode::SUCCESS
        }
        Err(error) => {
            eprintln!("Error: {error}");
            ExitCode::FAILURE
        }
    }
}

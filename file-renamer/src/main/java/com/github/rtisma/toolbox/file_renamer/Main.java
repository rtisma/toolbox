package com.github.rtisma.toolbox.file_renamer;

import com.github.rtisma.toolbox.file_renamer.cli.command.RootCommand;
import com.github.rtisma.toolbox.file_renamer.cli.config.CliConfig;
import com.google.common.base.Joiner;
import lombok.extern.slf4j.Slf4j;
import lombok.val;

@Slf4j
public class Main
{
  public static void main(String[] args) {
    log.info("Running command: {}", Joiner.on(" ").join(args));
    val config = new CliConfig(new RootCommand());
    config.commandLine()
        .setTrimQuotes(true)
        .setCaseInsensitiveEnumValuesAllowed(true)
        .execute(args);
  }
}

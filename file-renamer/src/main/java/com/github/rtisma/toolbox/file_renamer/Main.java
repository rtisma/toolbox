package com.github.rtisma.toolbox.file_renamer;

import com.github.rtisma.toolbox.file_renamer.cli.command.RootCommand;
import com.github.rtisma.toolbox.file_renamer.cli.config.CliConfig;
import lombok.val;

public class Main
{
  public static void main(String[] args) {
    val config = new CliConfig(new RootCommand());
    config.commandLine().execute(args);
  }
}

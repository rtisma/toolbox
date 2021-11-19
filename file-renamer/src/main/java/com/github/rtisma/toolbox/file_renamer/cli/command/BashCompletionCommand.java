package com.github.rtisma.toolbox.file_renamer.cli.command;

import static picocli.AutoComplete.bash;

import java.io.File;
import java.util.concurrent.Callable;

import com.github.rtisma.toolbox.file_renamer.cli.util.VersionProvider;
import lombok.NonNull;
import picocli.CommandLine;
import picocli.CommandLine.Command;
import picocli.CommandLine.Option;

@Command(
    name = "bash-completion",
    mixinStandardHelpOptions = true,
    versionProvider = VersionProvider.class,
    description = "Dump the bash-completion script.")
public class BashCompletionCommand implements Callable<Integer> {

  /** Dependencies */
  private final CommandLine commandLine;

  /** Parameters */
  @Option(
      names = {"-n", "--script-name"},
      required = true,
      paramLabel = "STRING",
      description = "Name of the entry point command")
  private String scriptName;

  @Option(
      names = {"-o", "--output-file"},
      required = false,
      paramLabel = "FILE",
      description = "Path of output auto-complete script")
  private File outFile;

  public BashCompletionCommand(@NonNull CommandLine commandLine) {
    this.commandLine = commandLine;
  }

  @Override
  public Integer call() throws Exception {
    /** Note: removed output to file, since this can cause confusion with docker paths */
    System.out.println(bash(scriptName, commandLine));
    return 0;
  }
}
